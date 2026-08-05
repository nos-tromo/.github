#!/usr/bin/env python3
"""Validate that every GitHub Actions ``uses:`` ref in a consumer is SHA-pinned.

Run from a consumer repo's root (or pass --consumer-root). Scans the repo's
workflow files (``.github/workflows/*.yml``/``*.yaml``) plus any composite
action definitions it ships (``action.yml``/``action.yaml`` at the root or
under ``actions/*/``) and requires each ``uses:`` reference to name an
immutable revision.

A tag or branch ref (``@v4``, ``@main``) resolves at run time, so whoever
controls the tag controls the code that executes in CI. Pinning to a full
commit SHA removes that seam: the ref is content-addressed and cannot be
moved under the consumer. The human-readable version stays in a trailing
comment, which Dependabot reads and rewrites when it bumps the pin::

    uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1

Accepted forms:

* ``./path`` - a local action from the same checkout; there is no remote
  ref to pin, the code is whatever the consumer already checked out.
* ``docker://image@sha256:<64 hex>`` - a container action pinned by image
  digest, the equivalent immutability guarantee for a registry ref.
* ``owner/repo[/path]@<40 hex>`` - a repository action or reusable workflow
  pinned to a full commit SHA.

Anything else (a tag, a branch, an abbreviated SHA, or no ``@ref`` at all)
is a violation and is reported with its file and line number.

Skip-when-absent: a consumer with no ``.github/workflows/`` directory has no
Actions surface to harden and is skipped rather than failed.

Parsing is hand-rolled line matching rather than YAML: like the other
validators here, this script must run in any consumer's environment with no
installed dependencies. Fully-commented lines are ignored, so a commented-out
``uses:`` never trips the check.

Exit 0 when every ref is pinned (or the check is skipped), 1 on violations.
Stdlib-only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
USES_RE = re.compile(r"^(?:-\s+)?uses:\s*(.+)$")


def _extract_uses_value(line: str) -> str | None:
    """Extract the action reference from a single workflow line.

    Recognizes ``uses: <ref>`` and the list-item form ``- uses: <ref>``,
    ignoring leading indentation. Surrounding quotes and a trailing
    ``# comment`` (the conventional home of the human-readable version) are
    stripped from the value.

    Args:
        line: One raw line from a workflow or action definition file.

    Returns:
        The bare action reference, or ``None`` if the line is a comment or
        does not declare a ``uses:`` key.
    """
    stripped = line.strip()
    if stripped.startswith("#"):
        return None
    match = USES_RE.match(stripped)
    if match is None:
        return None

    value = match.group(1).strip()
    if value[:1] in {'"', "'"}:
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            return value[1:end]
        return value[1:].strip()

    return value.split("#", 1)[0].strip()


def _is_pinned(value: str) -> bool:
    """Return whether an action reference names an immutable revision.

    Args:
        value: A bare ``uses:`` reference, e.g. ``actions/checkout@<sha>``.

    Returns:
        ``True`` for a local ``./path`` action, a ``docker://`` ref carrying
        an ``@sha256:`` digest, or an ``owner/repo[/path]`` ref whose ``@ref``
        is a full 40-character commit SHA; ``False`` otherwise.
    """
    if value.startswith("./"):
        return True

    _, sep, ref = value.rpartition("@")
    if not sep:
        return False

    if value.startswith("docker://"):
        return bool(DIGEST_RE.match(ref))

    return bool(SHA_RE.match(ref))


def _target_files(consumer_root: Path) -> list[Path]:
    """Collect the workflow and action-definition files to check.

    Args:
        consumer_root: The consumer repo root to inspect.

    Returns:
        Sorted paths of ``.github/workflows/*.yml|*.yaml``, a root-level
        ``action.yml|action.yaml``, and ``actions/*/action.yml|action.yaml``
        (composite actions the repo ships itself).
    """
    workflows = consumer_root / ".github" / "workflows"
    files: set[Path] = set()
    for name in ("action.yml", "action.yaml"):
        files.add(consumer_root / name)
        files.update(consumer_root.glob(f"actions/*/{name}"))
    for pattern in ("*.yml", "*.yaml"):
        files.update(workflows.glob(pattern))
    return sorted(p for p in files if p.is_file())


def _violations(path: Path) -> list[tuple[int, str]]:
    """Find unpinned action references in one file.

    Args:
        path: A workflow or action-definition file to scan.

    Returns:
        ``(line number, reference)`` pairs for every ``uses:`` value that is
        not pinned to an immutable revision. Line numbers are 1-based.
    """
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        value = _extract_uses_value(line)
        if value is not None and not _is_pinned(value):
            found.append((lineno, value))
    return found


def main() -> int:
    """Run the pin check and return a shell exit code.

    Scans the consumer rooted at ``--consumer-root`` (default ``.``). A repo
    with no ``.github/workflows/`` directory is skipped (see the module
    docstring).

    Returns:
        ``0`` when every ``uses:`` reference is pinned to an immutable
        revision, or the consumer has no workflows; ``1`` when any reference
        is unpinned.
    """
    p = argparse.ArgumentParser(
        description="Validate that every GitHub Actions `uses:` ref is pinned to a full commit SHA.",
    )
    p.add_argument("--consumer-root", type=Path, default=Path())
    args = p.parse_args()

    if not (args.consumer_root / ".github" / "workflows").is_dir():
        print("no .github/workflows/ directory; skipping (no Actions surface).")
        return 0

    files = _target_files(args.consumer_root)
    failures = [(path, lineno, value) for path in files for lineno, value in _violations(path)]

    if not failures:
        print(f"action pin check OK ({len(files)} file(s) scanned).")
        return 0

    print("action pin check FAILED - unpinned `uses:` refs found.\n", file=sys.stderr)
    for path, lineno, value in failures:
        print(f"{path}:{lineno}: unpinned action ref: {value}", file=sys.stderr)
    print(
        "\nTo fix: pin each ref to a full 40-character commit SHA and keep the\n"
        "version in a trailing comment, e.g.\n"
        "  uses: owner/repo@<full-sha> # vX.Y.Z\n"
        "Resolve a tag's SHA with: git ls-remote https://github.com/owner/repo refs/tags/vX.Y.Z",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
