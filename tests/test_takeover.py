# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Noa Resare
from __future__ import annotations

import base64
import gzip
import json
from collections.abc import Sequence

import pytest

from argo_takeover.takeover import (
    ResourceRef,
    TakeoverError,
    check_release,
    decode_release,
    parse_manifest,
    select_release_secret,
)

MANIFEST = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: other
---
# an empty document is skipped
"""

LIST_MANIFEST = """
apiVersion: v1
kind: List
items:
  - apiVersion: networking.k8s.io/v1
    kind: IngressClass
    metadata:
      name: ingress-class
  - apiVersion: v1
    kind: ConfigMap
    metadata:
      name: nested
---
apiVersion: v1
kind: ConfigMapList
items:
  - metadata:
      name: untyped
"""


def encode_release(release: dict[str, object], compress: bool = True) -> str:
    payload = json.dumps(release).encode()
    if compress:
        payload = gzip.compress(payload)
    return base64.b64encode(base64.b64encode(payload)).decode()


def release_secret(name: str, version: str, status: str, manifest: str) -> dict:
    return {
        "metadata": {
            "name": name,
            "labels": {"owner": "helm", "version": version, "status": status},
        },
        "data": {"release": encode_release({"manifest": manifest})},
    }


class FakeKubectl:
    """A kubectl stand-in backed by a dict of objects keyed by kind/name."""

    def __init__(self, secrets: list[dict], objects: dict[tuple[str, str], dict]):
        self.secrets = secrets
        self.objects = objects
        self.calls: list[Sequence[str]] = []

    def __call__(self, args: Sequence[str], input_data: str | None = None) -> str:
        self.calls.append(args)
        if args[1] == "secret":
            return json.dumps({"items": self.secrets})
        kind, name = args[1], args[2]
        try:
            return json.dumps(self.objects[(kind, name)])
        except KeyError:
            raise TakeoverError(f'{kind} "{name}" not found') from None


def tracked(annotations: dict[str, str] | None = None) -> dict:
    return {"metadata": {"annotations": annotations}}


def test_decode_release_gzipped():
    assert decode_release(encode_release({"manifest": "x"})) == {"manifest": "x"}


def test_decode_release_uncompressed():
    assert decode_release(encode_release({"manifest": "x"}, compress=False)) == {
        "manifest": "x"
    }


def test_decode_release_rejects_garbage():
    with pytest.raises(TakeoverError):
        decode_release(base64.b64encode(base64.b64encode(b"not json")).decode())


def test_select_release_secret_prefers_deployed_over_newer():
    superseded = release_secret("v1", "1", "superseded", "")
    deployed = release_secret("v2", "2", "deployed", "")
    pending = release_secret("v3", "3", "pending-upgrade", "")
    assert select_release_secret([superseded, deployed, pending]) is deployed


def test_select_release_secret_picks_highest_version_without_deployed():
    first = release_secret("v1", "1", "superseded", "")
    second = release_secret("v2", "2", "failed", "")
    assert select_release_secret([first, second]) is second


def test_select_release_secret_without_candidates():
    with pytest.raises(TakeoverError):
        select_release_secret([])


def test_parse_manifest_defaults_namespace():
    refs = parse_manifest(MANIFEST, "apps")
    assert refs == [
        ResourceRef("v1", "ConfigMap", "settings", "apps"),
        ResourceRef("apps/v1", "Deployment", "web", "other"),
    ]


def test_parse_manifest_unwraps_lists():
    assert parse_manifest(LIST_MANIFEST, "apps") == [
        ResourceRef("networking.k8s.io/v1", "IngressClass", "ingress-class", "apps"),
        ResourceRef("v1", "ConfigMap", "nested", "apps"),
        ResourceRef("v1", "ConfigMap", "untyped", "apps"),
    ]


def test_resource_arg_omits_version_for_core_resources():
    assert ResourceRef("v1", "ServiceAccount", "sa", None).resource_arg == (
        "ServiceAccount"
    )
    assert (
        ResourceRef("apps/v1", "Deployment", "d", None).resource_arg
        == "Deployment.v1.apps"
    )


def test_check_release_all_tracked():
    kubectl = FakeKubectl(
        [release_secret("v1", "1", "deployed", MANIFEST)],
        {
            ("ConfigMap", "settings"): tracked(
                {"argocd.argoproj.io/tracking-id": "app:/ConfigMap:apps/settings"}
            ),
            ("Deployment.v1.apps", "web"): tracked(
                {"argocd.argoproj.io/tracking-id": "app:apps/Deployment:other/web"}
            ),
        },
    )
    refs, untracked = check_release("apps", "demo", kubectl)
    assert len(refs) == 2
    assert untracked == []


def test_check_release_reports_missing_annotation_and_missing_object():
    kubectl = FakeKubectl(
        [release_secret("v1", "1", "deployed", MANIFEST)],
        {("ConfigMap", "settings"): tracked({"other": "annotation"})},
    )
    _, untracked = check_release("apps", "demo", kubectl)
    reasons = {u.ref.kind: u.reason for u in untracked}
    assert reasons["ConfigMap"] == "missing argocd.argoproj.io/tracking-id annotation"
    assert "not found" in reasons["Deployment"]


def test_check_release_handles_object_without_annotations():
    kubectl = FakeKubectl(
        [release_secret("v1", "1", "deployed", "")],
        {},
    )
    refs, untracked = check_release("apps", "demo", kubectl)
    assert refs == [] and untracked == []


def test_check_resource_passes_namespace_to_kubectl():
    kubectl = FakeKubectl(
        [release_secret("v1", "1", "deployed", MANIFEST)],
        {
            ("ConfigMap", "settings"): tracked({}),
            ("Deployment.v1.apps", "web"): tracked({}),
        },
    )
    check_release("apps", "demo", kubectl)
    namespaces = [
        call[call.index("-n") + 1] for call in kubectl.calls if call[1] != "secret"
    ]
    assert sorted(namespaces) == ["apps", "other"]
