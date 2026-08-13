# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Noa Resare
"""Remove Helm's leftover field ownership from objects taken over by Argo CD.

The mechanism is a server-side apply of an empty manifest as the field manager
``helm``. Server-side apply treats the applied manifest as the complete
statement of what a manager wants, so every field helm previously owned is
relinquished: fields co-owned by another manager (such as argocd-controller)
keep their value and transfer ownership, while fields solely owned by helm are
deleted. Owning nothing, helm's managedFields entry is then dropped entirely.

The fields that the apply will delete (those solely owned by helm) are
computed from the ``fieldsV1`` tries in managedFields. Beyond the well-known
Helm leftovers these are almost always values the apiserver defaulted in when
helm created the object; deleting them just makes the apiserver re-default
them on the same request, so they are reported as such rather than blocking
the cleanup. The after-check in ``value_changes`` catches the exceptions.
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

HELM_MANAGER = "helm"

MAP_OWNER = "."

FieldPath = tuple[str, ...]


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


def owned_paths(fields_v1: dict[str, Any], prefix: FieldPath = ()) -> set[FieldPath]:
    """The set of field paths a fieldsV1 trie claims ownership of.

    A leaf (empty dict) means the manager owns that field and everything below
    it. A ``.`` key means it owns the existence of the surrounding map.
    """
    paths: set[FieldPath] = set()
    for key, value in fields_v1.items():
        if key == MAP_OWNER:
            paths.add(prefix + (MAP_OWNER,))
        elif value:
            paths |= owned_paths(value, prefix + (key,))
        else:
            paths.add(prefix + (key,))
    return paths


def co_owned(path: FieldPath, others: set[FieldPath]) -> bool:
    """Whether another manager owns this path or a subtree containing it."""
    return any(path[:n] in others for n in range(1, len(path) + 1))


def expected_junk(path: FieldPath) -> bool:
    """Whether deleting this solely-helm-owned path is a known safe cleanup."""
    if path[-1] == MAP_OWNER:
        return True
    if len(path) == 3 and path[:2] == ("f:metadata", "f:labels"):
        return path[2] in ("f:app.kubernetes.io/managed-by", "f:helm.sh/chart")
    if len(path) == 3 and path[:2] == ("f:metadata", "f:annotations"):
        return path[2].startswith("f:meta.helm.sh/")
    return False


def render_path(path: FieldPath) -> str:
    return ".".join(p.removeprefix("f:") for p in path if p != MAP_OWNER)


def live_value(obj: Any, path: FieldPath) -> Any:
    """The value at a fieldsV1 path in a live object, None when absent."""
    node = obj
    for part in path:
        if part == MAP_OWNER:
            return node
        if part.startswith("f:") and isinstance(node, dict):
            node = node.get(part[2:])
        elif part.startswith("k:") and isinstance(node, list):
            keys = json.loads(part[2:])
            node = next(
                (
                    element
                    for element in node
                    if isinstance(element, dict)
                    and all(element.get(k) == v for k, v in keys.items())
                ),
                None,
            )
        elif part.startswith("v:"):
            return json.loads(part[2:])
        elif part.startswith("i:") and isinstance(node, list):
            index = int(part[2:])
            node = node[index] if index < len(node) else None
        else:
            return "<unresolved>"
        if node is None:
            return None
    return node


def get_with_managed_fields(ref: ResourceRef, kubectl: Kubectl) -> dict[str, Any]:
    args = ["get", ref.resource_arg, ref.name, "--show-managed-fields", "-o", "json"]
    if ref.namespace:
        args += ["-n", ref.namespace]
    return json.loads(kubectl(args))


def split_ownership(obj: dict[str, Any]) -> tuple[set[FieldPath], set[FieldPath]]:
    """The paths owned by helm and by everyone else, respectively."""
    helm: set[FieldPath] = set()
    others: set[FieldPath] = set()
    for entry in obj.get("metadata", {}).get("managedFields", []):
        paths = owned_paths(entry.get("fieldsV1", {}))
        if entry.get("manager") == HELM_MANAGER:
            helm |= paths
        else:
            others |= paths
    return helm, others


def has_helm_manager(obj: dict[str, Any]) -> bool:
    return any(
        entry.get("manager") == HELM_MANAGER
        for entry in obj.get("metadata", {}).get("managedFields", [])
    )


def apply_empty_as_helm(ref: ResourceRef, kubectl: Kubectl) -> None:
    skeleton: dict[str, Any] = {
        "apiVersion": ref.api_version,
        "kind": ref.kind,
        "metadata": {"name": ref.name},
    }
    args = ["apply", "--server-side", f"--field-manager={HELM_MANAGER}", "-f", "-"]
    if ref.namespace:
        skeleton["metadata"]["namespace"] = ref.namespace
        args += ["-n", ref.namespace]
    kubectl(args, input_data=json.dumps(skeleton))


def upgrade_helm_entries(
    ref: ResourceRef, obj: dict[str, Any], kubectl: Kubectl
) -> None:
    """Rewrite helm's Update managedFields entries to Apply.

    Fallback for apiservers that do not combine same-named Update and Apply
    managers during an apply — the same approach as client-go's csaupgrade.
    """
    entries = obj["metadata"]["managedFields"]
    upgraded = [
        {**e, "operation": "Apply"} if e.get("manager") == HELM_MANAGER else e
        for e in entries
    ]
    patch = [{"op": "replace", "path": "/metadata/managedFields", "value": upgraded}]
    args = ["patch", ref.resource_arg, ref.name, "--type=json", "-p", json.dumps(patch)]
    if ref.namespace:
        args += ["-n", ref.namespace]
    kubectl(args)


def describe_removal(obj: dict[str, Any], path: FieldPath) -> str:
    if expected_junk(path):
        return render_path(path)
    return (
        f"{render_path(path)} = {json.dumps(live_value(obj, path))} "
        "(probably a Kubernetes default, attributed to helm)"
    )


def cleanup_resource(
    ref: ResourceRef, kubectl: Kubectl, *, apply: bool
) -> CleanupResult:
    try:
        obj = get_with_managed_fields(ref, kubectl)
    except TakeoverError as e:
        return CleanupResult(ref, Status.FAILED, problems=(str(e),))

    annotations = obj.get("metadata", {}).get("annotations") or {}
    if TRACKING_ID_ANNOTATION not in annotations and not tracking_exempt(ref):
        return CleanupResult(
            ref,
            Status.NEEDS_REVIEW,
            problems=(f"not tracked by Argo CD ({TRACKING_ID_ANNOTATION} missing)",),
        )

    helm, others = split_ownership(obj)
    if not helm:
        return CleanupResult(ref, Status.CLEAN)

    sole = sorted(p for p in helm if not co_owned(p, others))
    removals = tuple(describe_removal(obj, p) for p in sole if p[-1] != MAP_OWNER)

    if not apply:
        return CleanupResult(ref, Status.WOULD_CLEAN, removals)

    try:
        apply_empty_as_helm(ref, kubectl)
        after = get_with_managed_fields(ref, kubectl)
        if has_helm_manager(after):
            upgrade_helm_entries(ref, after, kubectl)
            apply_empty_as_helm(ref, kubectl)
            after = get_with_managed_fields(ref, kubectl)
    except TakeoverError as e:
        return CleanupResult(ref, Status.FAILED, removals, (str(e),))
    if has_helm_manager(after):
        return CleanupResult(
            ref,
            Status.FAILED,
            removals,
            ("helm still appears in managedFields after cleanup",),
        )
    return CleanupResult(ref, Status.CLEANED, removals, value_changes(obj, after, sole))


def value_changes(
    before: dict[str, Any], after: dict[str, Any], paths: list[FieldPath]
) -> tuple[str, ...]:
    """Fields that ended up with a different value rather than deleted or re-defaulted.

    A relinquished field should either be gone (the Helm leftovers) or come
    back identical from apiserver defaulting; anything else is worth a look.
    """
    changes = []
    for path in paths:
        if path[-1] == MAP_OWNER:
            continue
        old, new = live_value(before, path), live_value(after, path)
        if new is not None and new != old:
            changes.append(
                f"value changed: {render_path(path)} "
                f"was {json.dumps(old)}, is now {json.dumps(new)}"
            )
    return tuple(changes)


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
