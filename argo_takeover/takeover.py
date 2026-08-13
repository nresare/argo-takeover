# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Noa Resare
"""Check that every object owned by a Helm release carries the Argo CD tracking id.

Helm stores the state of a release in a Secret of type ``helm.sh/release.v1``. The
``release`` key of that Secret holds the release object as base64(gzip(json)) which,
in turn, is base64 encoded by Kubernetes itself. The ``manifest`` member of the
release object is the rendered multi document YAML of everything the release
considers itself the owner of.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import subprocess
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

import yaml

TRACKING_ID_ANNOTATION = "argocd.argoproj.io/tracking-id"

GZIP_MAGIC = b"\x1f\x8b"


class TakeoverError(Exception):
    """Raised when the state of the cluster or the release cannot be determined."""


class Kubectl(Protocol):
    """Runs a kubectl invocation and returns its stdout."""

    def __call__(self, args: Sequence[str], input_data: str | None = None) -> str: ...


@dataclass(frozen=True)
class ResourceRef:
    """A reference to a single object in the cluster."""

    api_version: str
    kind: str
    name: str
    namespace: str | None

    @property
    def resource_arg(self) -> str:
        """The resource selector to pass to kubectl.

        Grouped resources use the unambiguous ``kind.version.group`` form. Core
        resources have no group, and kubectl would read the version in
        ``ServiceAccount.v1`` as one, so they are passed as the bare kind.
        """
        if "/" in self.api_version:
            group, _, version = self.api_version.partition("/")
            return f"{self.kind}.{version}.{group}"
        return self.kind

    def __str__(self) -> str:
        where = f"{self.namespace}/" if self.namespace else ""
        return f"{self.api_version} {self.kind} {where}{self.name}"


@dataclass(frozen=True)
class Untracked:
    """An object that is not (yet) tracked by Argo CD."""

    ref: ResourceRef
    reason: str

    def __str__(self) -> str:
        return f"{self.ref}: {self.reason}"


def run_kubectl(args: Sequence[str], input_data: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        check=False,
        input=input_data,
    )
    if result.returncode != 0:
        raise TakeoverError(
            f"kubectl {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def decode_release(encoded: str) -> dict[str, Any]:
    """Decode the ``release`` member of a Helm release Secret."""
    try:
        outer = base64.b64decode(encoded, validate=True)
        payload = base64.b64decode(outer, validate=True)
    except (binascii.Error, ValueError) as e:
        raise TakeoverError(f"release data is not valid base64: {e}") from e
    if payload[:2] == GZIP_MAGIC:
        payload = gzip.decompress(payload)
    try:
        release = json.loads(payload)
    except json.JSONDecodeError as e:
        raise TakeoverError(f"release data is not valid JSON: {e}") from e
    if not isinstance(release, dict):
        raise TakeoverError("release data is not a JSON object")
    return release


def select_release_secret(secrets: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pick the newest revision, preferring the one Helm considers deployed."""
    candidates = list(secrets)
    if not candidates:
        raise TakeoverError("no Helm release secret found")

    def version(secret: dict[str, Any]) -> int:
        labels = secret.get("metadata", {}).get("labels", {})
        try:
            return int(labels.get("version", 0))
        except ValueError:
            return 0

    deployed = [
        s
        for s in candidates
        if s.get("metadata", {}).get("labels", {}).get("status") == "deployed"
    ]
    return max(deployed or candidates, key=version)


def load_release(namespace: str, release: str, kubectl: Kubectl) -> dict[str, Any]:
    output = kubectl(
        [
            "get",
            "secret",
            "-n",
            namespace,
            "-l",
            f"owner=helm,name={release}",
            "-o",
            "json",
        ]
    )
    secrets = json.loads(output).get("items", [])
    secret = select_release_secret(secrets)
    encoded = secret.get("data", {}).get("release")
    if not encoded:
        name = secret.get("metadata", {}).get("name", "<unknown>")
        raise TakeoverError(f"secret {name} has no release data")
    return decode_release(encoded)


def _expand(doc: Any) -> Iterable[dict[str, Any]]:
    """Yield the individual objects of a document, unwrapping List kinds."""
    if not isinstance(doc, dict):
        return
    kind = doc.get("kind")
    items = doc.get("items")
    if isinstance(kind, str) and kind.endswith("List") and isinstance(items, list):
        # A typed list such as ConfigMapList may leave its items untyped.
        defaults: dict[str, Any] = {}
        if kind != "List":
            defaults = {
                "apiVersion": doc.get("apiVersion"),
                "kind": kind[: -len("List")],
            }
        for item in items:
            if isinstance(item, dict):
                item = {**defaults, **item}
            yield from _expand(item)
        return
    yield doc


def parse_manifest(manifest: str, default_namespace: str) -> list[ResourceRef]:
    """Turn the rendered manifest of a release into references to cluster objects."""
    refs: list[ResourceRef] = []
    for doc in yaml.safe_load_all(manifest):
        for obj in _expand(doc):
            metadata = obj.get("metadata") or {}
            name = metadata.get("name")
            api_version = obj.get("apiVersion")
            kind = obj.get("kind")
            if not (name and api_version and kind):
                continue
            refs.append(
                ResourceRef(
                    api_version=api_version,
                    kind=kind,
                    name=name,
                    namespace=metadata.get("namespace") or default_namespace,
                )
            )
    return refs


def tracking_exempt(ref: ResourceRef) -> bool:
    """Whether Argo CD tracks this object without annotating it.

    Argo CD deliberately skips the tracking annotation on the
    CustomResourceDefinitions it applies, so its absence says nothing about
    whether the takeover happened: https://github.com/argoproj/argo-cd/issues/17400
    """
    return ref.kind == "CustomResourceDefinition" and ref.api_version.startswith(
        "apiextensions.k8s.io/"
    )


def check_resource(ref: ResourceRef, kubectl: Kubectl) -> Untracked | None:
    """Return None when the object exists and carries the tracking id annotation."""
    args = ["get", ref.resource_arg, ref.name, "-o", "json"]
    if ref.namespace:
        args += ["-n", ref.namespace]
    try:
        obj = json.loads(kubectl(args))
    except TakeoverError as e:
        return Untracked(ref, str(e))
    annotations = obj.get("metadata", {}).get("annotations") or {}
    if TRACKING_ID_ANNOTATION in annotations or tracking_exempt(ref):
        return None
    return Untracked(ref, f"missing {TRACKING_ID_ANNOTATION} annotation")


def check_release(
    namespace: str, release: str, kubectl: Kubectl = run_kubectl
) -> tuple[list[ResourceRef], list[Untracked]]:
    """Check every object owned by ``release``, returning all of them and the failures."""
    release_data = load_release(namespace, release, kubectl)
    refs = parse_manifest(release_data.get("manifest") or "", namespace)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda ref: check_resource(ref, kubectl), refs)
    return refs, [r for r in results if r is not None]
