# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Noa Resare
from __future__ import annotations

import base64
import copy
import gzip
import json
from collections.abc import Sequence

from argo_takeover.cleanup import (
    Status,
    cleanup_manifest,
    cleanup_release,
    delete_release_secrets,
    helm_matches,
    json_pointer_escape,
)
from argo_takeover.takeover import TakeoverError


def deployment(*, labels: dict | None = None, annotations: dict | None = None) -> dict:
    default_labels = {
        "app.kubernetes.io/managed-by": "Helm",
        "helm.sh/chart": "web-1.2.3",
        "app.kubernetes.io/name": "web",
    }
    default_annotations = {
        "meta.helm.sh/release-name": "demo",
        "meta.helm.sh/release-namespace": "apps",
        "argocd.argoproj.io/tracking-id": "demo:apps/Deployment:apps/web",
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "web",
            "namespace": "apps",
            "labels": labels if labels is not None else default_labels,
            "annotations": annotations
            if annotations is not None
            else default_annotations,
        },
        "spec": {"replicas": 2},
    }


class FakeCluster:
    """A one-object cluster whose `patch --type=json` mimics the apiserver."""

    def __init__(self, obj: dict):
        self.obj = obj
        self.patches: list[list[dict]] = []
        self.gets = 0
        self.deletes: list[list[str]] = []

    def __call__(self, args: Sequence[str], input_data: str | None = None) -> str:
        if args[0] == "get" and args[1] == "secret":
            manifest = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
            release = {"manifest": manifest}
            encoded = base64.b64encode(
                base64.b64encode(gzip.compress(json.dumps(release).encode()))
            ).decode()
            secret = {
                "metadata": {"labels": {"version": "1", "status": "deployed"}},
                "data": {"release": encoded},
            }
            return json.dumps({"items": [secret]})
        if args[0] == "get":
            self.gets += 1
            return json.dumps(self.obj)
        if args[0] == "patch":
            ops = json.loads(args[args.index("-p") + 1])
            self.patches.append(ops)
            self._apply_json_patch(ops)
            return ""
        if args[0] == "delete":
            self.deletes.append(list(args))
            return (
                "secret/sh.helm.release.v1.demo.v1\nsecret/sh.helm.release.v1.demo.v2\n"
            )
        raise TakeoverError(f"unexpected kubectl call: {args}")

    def _apply_json_patch(self, ops: list[dict]) -> None:
        for op in ops:
            assert op["op"] == "remove"
            parts = op["path"].split("/")[1:]
            key = parts[-1].replace("~1", "/").replace("~0", "~")
            container = self.obj
            for part in parts[:-1]:
                container = container[part]
            del container[key]


def test_helm_matches_finds_key_or_value_hits_case_insensitively():
    mapping = {
        "app.kubernetes.io/managed-by": "Helm",
        "helm.sh/chart": "web-1.2.3",
        "app.kubernetes.io/name": "web",
        "some.other/HELM-ish": "unrelated",
    }
    assert helm_matches(mapping) == [
        "app.kubernetes.io/managed-by",
        "helm.sh/chart",
        "some.other/HELM-ish",
    ]


def test_helm_matches_handles_empty_and_none():
    assert helm_matches(None) == []
    assert helm_matches({}) == []
    assert helm_matches({"app.kubernetes.io/name": "web"}) == []


def test_json_pointer_escape():
    assert (
        json_pointer_escape("meta.helm.sh/release-name") == "meta.helm.sh~1release-name"
    )
    assert json_pointer_escape("a~b") == "a~0b"


def test_dry_run_reports_without_mutating():
    cluster = FakeCluster(deployment())
    (result,) = cleanup_release("apps", "demo", cluster)
    assert result.status is Status.WOULD_CLEAN
    assert result.removals == (
        "labels.app.kubernetes.io/managed-by",
        "labels.helm.sh/chart",
        "annotations.meta.helm.sh/release-name",
        "annotations.meta.helm.sh/release-namespace",
    )
    assert cluster.patches == []


def test_apply_removes_only_helm_matching_labels_and_annotations():
    cluster = FakeCluster(deployment())
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.CLEANED
    assert len(cluster.patches) == 1
    metadata = cluster.obj["metadata"]
    assert metadata["labels"] == {"app.kubernetes.io/name": "web"}
    assert metadata["annotations"] == {
        "argocd.argoproj.io/tracking-id": "demo:apps/Deployment:apps/web"
    }
    # untouched
    assert cluster.obj["spec"] == {"replicas": 2}


def test_incident_regression_configmap_data_and_service_selector_survive_cleanup():
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "app-config",
            "namespace": "apps",
            "labels": {
                "app.kubernetes.io/managed-by": "Helm",
                "helm.sh/chart": "app-1.0.0",
            },
            "annotations": {
                "meta.helm.sh/release-name": "demo",
                "meta.helm.sh/release-namespace": "apps",
                "argocd.argoproj.io/tracking-id": "demo:/ConfigMap:apps/app-config",
            },
        },
        "data": {"app.conf": "real running config\nkey=value\n"},
    }
    before_data = copy.deepcopy(configmap["data"])
    cluster = FakeCluster(configmap)
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.CLEANED
    assert cluster.obj["data"] == before_data
    assert "app.kubernetes.io/managed-by" not in cluster.obj["metadata"]["labels"]
    assert "helm.sh/chart" not in cluster.obj["metadata"]["labels"]

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "app-headless",
            "namespace": "apps",
            "labels": {
                "app.kubernetes.io/managed-by": "Helm",
                "helm.sh/chart": "app-1.0.0",
            },
            "annotations": {
                "meta.helm.sh/release-name": "demo",
                "meta.helm.sh/release-namespace": "apps",
                "argocd.argoproj.io/tracking-id": "demo:/Service:apps/app-headless",
            },
        },
        "spec": {
            "clusterIP": "None",
            "selector": {"app": "app", "role": "member"},
            "ports": [{"port": 7000, "name": "gossip"}],
        },
    }
    before_spec = copy.deepcopy(service["spec"])
    cluster2 = FakeCluster(service)
    (result2,) = cleanup_release("apps", "demo", cluster2, apply=True)
    assert result2.status is Status.CLEANED
    assert cluster2.obj["spec"] == before_spec
    assert "app.kubernetes.io/managed-by" not in cluster2.obj["metadata"]["labels"]
    assert "helm.sh/chart" not in cluster2.obj["metadata"]["labels"]


def test_immutable_field_resources_now_clean_successfully():
    crb = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {
            "name": "app-binding",
            "labels": {
                "app.kubernetes.io/managed-by": "Helm",
                "helm.sh/chart": "app-1.0.0",
            },
            "annotations": {
                "meta.helm.sh/release-name": "demo",
                "meta.helm.sh/release-namespace": "apps",
                "argocd.argoproj.io/tracking-id": (
                    "demo:rbac.authorization.k8s.io/ClusterRoleBinding:app-binding"
                ),
            },
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "app-role",
        },
        "subjects": [{"kind": "ServiceAccount", "name": "app", "namespace": "apps"}],
    }
    before_role_ref = copy.deepcopy(crb["roleRef"])
    cluster = FakeCluster(crb)
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.CLEANED
    assert cluster.obj["roleRef"] == before_role_ref
    assert "app.kubernetes.io/managed-by" not in cluster.obj["metadata"]["labels"]


def test_untracked_object_is_not_touched():
    cluster = FakeCluster(
        deployment(
            annotations={
                "meta.helm.sh/release-name": "demo",
                "meta.helm.sh/release-namespace": "apps",
            }
        )
    )
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.NEEDS_REVIEW
    assert cluster.patches == []


def test_cleanup_manifest_uses_rendered_yaml_instead_of_the_secret():
    cluster = FakeCluster(deployment())
    manifest = """
---
# Source: demo/templates/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
"""
    (result,) = cleanup_manifest("apps", manifest, cluster, apply=True)
    assert result.status is Status.CLEANED
    assert "app.kubernetes.io/managed-by" not in cluster.obj["metadata"]["labels"]
    secret_reads = [c for c in cluster.deletes if c[1] == "secret"]
    assert secret_reads == []


def test_delete_release_secrets():
    cluster = FakeCluster(deployment())
    deleted = delete_release_secrets("apps", "demo", cluster)
    assert deleted == [
        "secret/sh.helm.release.v1.demo.v1",
        "secret/sh.helm.release.v1.demo.v2",
    ]
    (call,) = cluster.deletes
    assert call == [
        "delete",
        "secret",
        "-n",
        "apps",
        "-l",
        "owner=helm,name=demo",
        "-o",
        "name",
    ]


def test_untracked_crd_is_cleaned_anyway():
    crd = deployment(
        annotations={
            "meta.helm.sh/release-name": "demo",
            "meta.helm.sh/release-namespace": "apps",
        }
    )
    crd["apiVersion"] = "apiextensions.k8s.io/v1"
    crd["kind"] = "CustomResourceDefinition"
    cluster = FakeCluster(crd)
    manifest = (
        "apiVersion: apiextensions.k8s.io/v1\n"
        "kind: CustomResourceDefinition\n"
        "metadata:\n  name: certificates.cert-manager.io\n"
    )
    (result,) = cleanup_manifest("kube-system", manifest, cluster, apply=True)
    assert result.status is Status.CLEANED
    assert "app.kubernetes.io/managed-by" not in cluster.obj["metadata"]["labels"]


def test_object_without_helm_labels_or_annotations_is_clean():
    cluster = FakeCluster(
        deployment(
            labels={"app.kubernetes.io/name": "web"},
            annotations={
                "argocd.argoproj.io/tracking-id": "demo:apps/Deployment:apps/web"
            },
        )
    )
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.CLEAN
    assert cluster.patches == []
