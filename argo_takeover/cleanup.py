# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Noa Resare
"""Remove Helm's leftover labels and annotations from objects taken over by Argo CD.

Earlier versions of this tool relinquished helm's ownership via a server-side
apply of an empty manifest under the ``helm`` field manager, relying on
``managedFields`` to know which fields were solely helm's to release. That
trust broke once the Argo CD instance driving these objects switched to
client-side apply for speed: client-side apply does not reliably reassert
per-field ownership away from helm's original entries, so a field could still
show as solely helm-owned in ``managedFields`` even though Argo CD had long
since taken over its actual value. Releasing that "ownership" then deleted the
field outright instead of handing it off, which is exactly what should never
happen to values Argo CD is depending on.

The mechanism here is deliberately narrow instead: only labels and annotations
whose key or value contains "helm" (case-insensitively) are removed, via a
targeted JSON patch that names exactly those keys. Nothing else on the object
is touched, so there is nothing left to trust about the ownership graph.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from argo_takeover.takeover import (
    TRACKING_ID_ANNOTATION,
    Kubectl,
    ResourceRef,
    TakeoverError,
    load_release,
    parse_manifest,
    run_kubectl,
    tracking_exempt,
)


class Status(StrEnum):
    CLEAN = "clean"
    WOULD_CLEAN = "would clean"
    CLEANED = "cleaned"
    NEEDS_REVIEW = "needs review"
    FAILED = "failed"


@dataclass(frozen=True)
class CleanupResult:
    ref: ResourceRef
    status: Status
    removals: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def mentions_helm(text: str) -> bool:
    return "helm" in text.lower()


def helm_matches(mapping: dict[str, Any] | None) -> list[str]:
    """Keys of a labels/annotations map whose key or value mentions helm."""
    if not mapping:
        return []
    return sorted(
        key
        for key, value in mapping.items()
        if mentions_helm(key) or mentions_helm(str(value))
    )


def json_pointer_escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def remove_patch_ops(
    label_keys: list[str], annotation_keys: list[str]
) -> list[dict[str, str]]:
    ops = [
        {"op": "remove", "path": f"/metadata/labels/{json_pointer_escape(k)}"}
        for k in label_keys
    ]
    ops += [
        {"op": "remove", "path": f"/metadata/annotations/{json_pointer_escape(k)}"}
        for k in annotation_keys
    ]
    return ops


def get_object(ref: ResourceRef, kubectl: Kubectl) -> dict[str, Any]:
    args = ["get", ref.resource_arg, ref.name, "-o", "json"]
    if ref.namespace:
        args += ["-n", ref.namespace]
    return json.loads(kubectl(args))


def patch_remove(ref: ResourceRef, ops: list[dict[str, str]], kubectl: Kubectl) -> None:
    args = ["patch", ref.resource_arg, ref.name, "--type=json", "-p", json.dumps(ops)]
    if ref.namespace:
        args += ["-n", ref.namespace]
    kubectl(args)


def cleanup_resource(
    ref: ResourceRef, kubectl: Kubectl, *, apply: bool
) -> CleanupResult:
    try:
        obj = get_object(ref, kubectl)
    except TakeoverError as e:
        return CleanupResult(ref, Status.FAILED, problems=(str(e),))

    metadata = obj.get("metadata", {})
    annotations = metadata.get("annotations") or {}
    if TRACKING_ID_ANNOTATION not in annotations and not tracking_exempt(ref):
        return CleanupResult(
            ref,
            Status.NEEDS_REVIEW,
            problems=(f"not tracked by Argo CD ({TRACKING_ID_ANNOTATION} missing)",),
        )

    labels = metadata.get("labels") or {}
    label_keys = helm_matches(labels)
    annotation_keys = helm_matches(annotations)
    if not label_keys and not annotation_keys:
        return CleanupResult(ref, Status.CLEAN)

    removals = tuple(
        [f"labels.{k}" for k in label_keys]
        + [f"annotations.{k}" for k in annotation_keys]
    )

    if not apply:
        return CleanupResult(ref, Status.WOULD_CLEAN, removals)

    try:
        patch_remove(ref, remove_patch_ops(label_keys, annotation_keys), kubectl)
    except TakeoverError as e:
        return CleanupResult(ref, Status.FAILED, removals, (str(e),))
    return CleanupResult(ref, Status.CLEANED, removals)


def cleanup_refs(
    refs: list[ResourceRef], kubectl: Kubectl, *, apply: bool
) -> list[CleanupResult]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(
            lambda ref: cleanup_resource(ref, kubectl, apply=apply), refs
        )
    return list(results)


def cleanup_release(
    namespace: str,
    release: str,
    kubectl: Kubectl = run_kubectl,
    *,
    apply: bool = False,
) -> list[CleanupResult]:
    """Clean every object owned by ``release``; a dry run unless ``apply``."""
    release_data = load_release(namespace, release, kubectl)
    refs = parse_manifest(release_data.get("manifest") or "", namespace)
    return cleanup_refs(refs, kubectl, apply=apply)


def cleanup_manifest(
    namespace: str,
    manifest: str,
    kubectl: Kubectl = run_kubectl,
    *,
    apply: bool = False,
) -> list[CleanupResult]:
    """Clean every object in a rendered manifest, e.g. ``helm template`` output.

    For when the release secret is already gone: only the object references
    are taken from the manifest, so a re-render does not need to reproduce the
    installed values exactly — it just has to name the same objects.
    """
    refs = parse_manifest(manifest, namespace)
    return cleanup_refs(refs, kubectl, apply=apply)


def delete_release_secrets(
    namespace: str, release: str, kubectl: Kubectl = run_kubectl
) -> list[str]:
    """Delete every revision of the Helm release's state secrets.

    Once these are gone the release no longer exists as far as Helm is
    concerned, so a stray ``helm upgrade`` or ``helm uninstall`` cannot touch
    the objects Argo CD now manages.
    """
    output = kubectl(
        [
            "delete",
            "secret",
            "-n",
            namespace,
            "-l",
            f"owner=helm,name={release}",
            "-o",
            "name",
        ]
    )
    return [line for line in output.splitlines() if line]
