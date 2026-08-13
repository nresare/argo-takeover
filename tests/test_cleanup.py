# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Noa Resare
from __future__ import annotations

import base64
import gzip
import json
from collections.abc import Sequence

from argo_takeover.cleanup import (
    Status,
    cleanup_release,
    co_owned,
    delete_release_secrets,
    expected_junk,
    live_value,
    owned_paths,
    render_path,
    value_changes,
)
from argo_takeover.takeover import TakeoverError

HELM_FIELDS = {
    "f:metadata": {
        "f:labels": {".": {}, "f:app.kubernetes.io/managed-by": {}},
        "f:annotations": {
            "f:meta.helm.sh/release-name": {},
            "f:meta.helm.sh/release-namespace": {},
        },
    },
    "f:spec": {"f:replicas": {}},
}

ARGO_FIELDS = {
    "f:metadata": {
        "f:annotations": {"f:argocd.argoproj.io/tracking-id": {}},
        "f:labels": {"f:app.kubernetes.io/name": {}},
    },
    "f:spec": {"f:replicas": {}},
}


def helm_entry(fields: dict, operation: str = "Update") -> dict:
    return {"manager": "helm", "operation": operation, "fieldsV1": fields}


def argo_entry() -> dict:
    return {
        "manager": "argocd-controller",
        "operation": "Update",
        "fieldsV1": ARGO_FIELDS,
    }


def deployment(managed_fields: list[dict], tracked: bool = True) -> dict:
    annotations = {
        "meta.helm.sh/release-name": "demo",
        "meta.helm.sh/release-namespace": "apps",
    }
    if tracked:
        annotations["argocd.argoproj.io/tracking-id"] = "demo:apps/Deployment:apps/web"
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "web",
            "namespace": "apps",
            "labels": {
                "app.kubernetes.io/managed-by": "Helm",
                "app.kubernetes.io/name": "web",
            },
            "annotations": annotations,
            "managedFields": managed_fields,
        },
        "spec": {"replicas": 2},
    }


class FakeCluster:
    """A one-object cluster that mimics the apiserver's apply semantics."""

    def __init__(self, obj: dict, combines_managers: bool = True):
        self.obj = obj
        self.combines_managers = combines_managers
        self.applies: list[dict] = []
        self.patches = 0
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
            return json.dumps(self.obj)
        if args[0] == "apply":
            assert input_data is not None
            self.applies.append(json.loads(input_data))
            entries = self.obj["metadata"]["managedFields"]
            can_relinquish = self.combines_managers or any(
                e["manager"] == "helm" and e["operation"] == "Apply" for e in entries
            )
            if can_relinquish:
                self._strip_helm()
            return ""
        if args[0] == "patch":
            patch = json.loads(args[args.index("-p") + 1])
            self.obj["metadata"]["managedFields"] = patch[0]["value"]
            self.patches += 1
            return ""
        if args[0] == "delete":
            self.deletes.append(list(args))
            return (
                "secret/sh.helm.release.v1.demo.v1\nsecret/sh.helm.release.v1.demo.v2\n"
            )
        raise TakeoverError(f"unexpected kubectl call: {args}")

    def _strip_helm(self) -> None:
        metadata = self.obj["metadata"]
        metadata["managedFields"] = [
            e for e in metadata["managedFields"] if e["manager"] != "helm"
        ]
        metadata["labels"].pop("app.kubernetes.io/managed-by", None)
        for key in list(metadata["annotations"]):
            if key.startswith("meta.helm.sh/"):
                del metadata["annotations"][key]


def test_owned_paths_flattens_the_trie():
    assert owned_paths(HELM_FIELDS) == {
        ("f:metadata", "f:labels", "."),
        ("f:metadata", "f:labels", "f:app.kubernetes.io/managed-by"),
        ("f:metadata", "f:annotations", "f:meta.helm.sh/release-name"),
        ("f:metadata", "f:annotations", "f:meta.helm.sh/release-namespace"),
        ("f:spec", "f:replicas"),
    }


def test_co_owned_matches_exact_path_and_owned_prefix():
    others = {("f:spec", "f:replicas"), ("f:data",)}
    assert co_owned(("f:spec", "f:replicas"), others)
    assert co_owned(("f:data", "f:key"), others)
    assert not co_owned(("f:metadata", "f:labels", "f:x"), others)


def test_expected_junk():
    assert expected_junk(("f:metadata", "f:labels", "f:app.kubernetes.io/managed-by"))
    assert expected_junk(("f:metadata", "f:labels", "f:helm.sh/chart"))
    assert expected_junk(("f:metadata", "f:annotations", "f:meta.helm.sh/release-name"))
    assert expected_junk(("f:metadata", "f:labels", "."))
    assert not expected_junk(("f:spec", "f:replicas"))
    assert not expected_junk(("f:metadata", "f:labels", "f:custom"))


def test_render_path():
    assert render_path(("f:metadata", "f:labels", "f:a/b")) == "metadata.labels.a/b"


def test_live_value_resolves_keyed_list_items():
    obj = {
        "spec": {
            "ports": [
                {"port": 80, "protocol": "TCP", "name": "http"},
                {"port": 443, "protocol": "TCP", "name": "https"},
            ]
        }
    }
    path = ("f:spec", "f:ports", 'k:{"port":443,"protocol":"TCP"}', "f:name")
    assert live_value(obj, path) == "https"
    missing = ("f:spec", "f:ports", 'k:{"port":8080,"protocol":"TCP"}', "f:name")
    assert live_value(obj, missing) is None
    assert live_value(obj, ("f:spec", "f:absent")) is None


def test_value_changes_ignores_deleted_and_identical_fields():
    before = {"metadata": {"labels": {"a": "1"}}, "spec": {"x": "same", "y": "old"}}
    after = {"metadata": {"labels": {}}, "spec": {"x": "same", "y": "new"}}
    paths = [
        ("f:metadata", "f:labels", "f:a"),  # deleted: fine
        ("f:spec", "f:x"),  # re-defaulted to the same value: fine
        ("f:spec", "f:y"),  # changed: reported
    ]
    assert value_changes(before, after, paths) == (
        'value changed: spec.y was "old", is now "new"',
    )


def test_dry_run_reports_without_mutating():
    cluster = FakeCluster(deployment([helm_entry(HELM_FIELDS), argo_entry()]))
    (result,) = cleanup_release("apps", "demo", cluster)
    assert result.status is Status.WOULD_CLEAN
    assert result.removals == (
        "metadata.annotations.meta.helm.sh/release-name",
        "metadata.annotations.meta.helm.sh/release-namespace",
        "metadata.labels.app.kubernetes.io/managed-by",
    )
    assert cluster.applies == []


def test_apply_cleans_and_verifies():
    cluster = FakeCluster(deployment([helm_entry(HELM_FIELDS), argo_entry()]))
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.CLEANED
    assert len(cluster.applies) == 1
    assert cluster.applies[0] == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "apps"},
    }
    assert "app.kubernetes.io/managed-by" not in cluster.obj["metadata"]["labels"]
    assert cluster.obj["metadata"]["labels"] == {"app.kubernetes.io/name": "web"}


def test_apply_falls_back_to_upgrading_the_manager_entry():
    cluster = FakeCluster(
        deployment([helm_entry(HELM_FIELDS), argo_entry()]), combines_managers=False
    )
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.CLEANED
    assert cluster.patches == 1
    assert len(cluster.applies) == 2


def test_unexpected_sole_ownership_is_labeled_and_cleaned():
    fields = {**HELM_FIELDS, "f:spec": {"f:replicas": {}, "f:paused": {}}}
    cluster = FakeCluster(deployment([helm_entry(fields), argo_entry()]))
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.CLEANED
    labeled = [r for r in result.removals if "spec.paused" in r]
    assert labeled == [
        "spec.paused = null (probably a Kubernetes default, attributed to helm)"
    ]
    assert not any(
        "Kubernetes default" in r for r in result.removals if "meta.helm" in r
    )


def test_untracked_object_is_not_touched():
    cluster = FakeCluster(
        deployment([helm_entry(HELM_FIELDS), argo_entry()], tracked=False)
    )
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.NEEDS_REVIEW
    assert cluster.applies == []


def test_delete_release_secrets():
    cluster = FakeCluster(deployment([argo_entry()]))
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


def test_object_without_helm_manager_is_clean():
    cluster = FakeCluster(deployment([argo_entry()]))
    (result,) = cleanup_release("apps", "demo", cluster, apply=True)
    assert result.status is Status.CLEAN
    assert cluster.applies == []
