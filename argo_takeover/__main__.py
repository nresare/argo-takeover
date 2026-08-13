# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Noa Resare
"""Command line entry point: ``uv run argo-takeover <command> <namespace> <release>``."""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from argo_takeover.cleanup import Status, cleanup_release, delete_release_secrets
from argo_takeover.takeover import TakeoverError, check_release


def run_check(namespace: str, release: str) -> int:
    refs, untracked = check_release(namespace, release)
    if not refs:
        print(f"release {release} owns no objects", file=sys.stderr)
        return 2
    if untracked:
        print(f"{len(untracked)} of {len(refs)} objects not taken over by Argo CD:")
        for item in untracked:
            print(f"  {item}")
        return 1
    print(f"takeover successful: all {len(refs)} objects are tracked by Argo CD")
    return 0


def run_cleanup(namespace: str, release: str, apply: bool) -> int:
    results = cleanup_release(namespace, release, apply=apply)
    if not results:
        print(f"release {release} owns no objects", file=sys.stderr)
        return 2
    for result in results:
        print(f"{result.status}: {result.ref}")
        for removal in result.removals:
            verb = "removed" if result.status is Status.CLEANED else "would remove"
            print(f"    {verb} {removal}")
        for problem in result.problems:
            print(f"    {problem}")
    counts = Counter(r.status for r in results)
    print(", ".join(f"{count} {status}" for status, count in counts.items()))
    if not apply and Status.WOULD_CLEAN in counts:
        print("dry run: re-run with --apply to modify the cluster")
    ok = {Status.CLEAN, Status.CLEANED if apply else Status.WOULD_CLEAN}
    all_ok = set(counts) <= ok
    if apply and all_ok:
        for name in delete_release_secrets(namespace, release):
            print(f"deleted {name}")
    elif apply:
        print("helm release secrets kept: not every object is clean")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="argo-takeover",
        description=(
            "Verify and complete the takeover of a Helm release's objects by Argo CD."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check",
        help="verify that every object owned by the release is tracked by Argo CD",
    )
    cleanup = subparsers.add_parser(
        "cleanup",
        help=(
            "remove Helm's leftover labels, annotations and field ownership, "
            "then delete the Helm release secrets"
        ),
    )
    cleanup.add_argument(
        "--apply",
        action="store_true",
        help="modify the cluster (default is a dry run)",
    )
    for sub in (check, cleanup):
        sub.add_argument("namespace", help="namespace holding the Helm release")
        sub.add_argument("release", help="name of the Helm release")

    args = parser.parse_args(argv)

    try:
        if args.command == "check":
            return run_check(args.namespace, args.release)
        return run_cleanup(args.namespace, args.release, args.apply)
    except TakeoverError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
