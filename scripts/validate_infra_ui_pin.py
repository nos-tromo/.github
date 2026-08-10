#!/usr/bin/env python3
"""Validate that a consumer pins ``@infra/ui`` to a full commit SHA.

Run from a consumer repo's root (or pass --consumer-root). Reads the
frontend's ``package.json`` and, when present, its ``pnpm-lock.yaml``, and
requires every reference to the ``nos-tromo/infra-ui`` repository to name an
immutable revision.

The four app frontends consume ``@infra/ui`` as a codeload tarball URL. A
URL naming a tag (``.../tar.gz/v0.10.0``) resolves at install time, so
whoever controls the tag controls the code that ships in every SPA bundle —
and pnpm records no integrity hash for tarball URLs, so the lockfile does
not catch a moved tag. Pinning the commit SHA the tag points to removes
that seam, exactly as ``validate_action_pins.py`` does for Actions refs::

    "@infra/ui": "https://codeload.github.com/nos-tromo/infra-ui/tar.gz/<full-sha>"

The human-readable version is not lost: pnpm resolves the tarball's own
``package.json`` and records ``version: X.Y.Z`` in the lockfile.

Checked forms:

* ``package.json``: every dependency section's ``@infra/ui`` value must be
  a ``https://codeload.github.com/nos-tromo/infra-ui/tar.gz/<40 hex>`` URL.
  The ``github:...#tag`` shorthand is rejected twice over — it is mutable
  *and* Dependabot rewrites it to a git+SSH lockfile entry that breaks
  keyless CI and git-less Docker builds.
* ``pnpm-lock.yaml``: any line mentioning ``nos-tromo/infra-ui`` must carry
  a 40-hex revision, so a stale lockfile cannot keep installing a tag ref
  after the manifest was fixed.

Skip-when-absent: a consumer with no ``<frontend-dir>/package.json``, or one
whose manifest does not depend on ``@infra/ui``, has nothing to pin and is
skipped rather than failed.

Exit 0 when every reference is pinned (or the check is skipped), 1 on
violations. Stdlib-only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PINNED_URL_RE = re.compile(r"^https://codeload\.github\.com/nos-tromo/infra-ui/tar\.gz/[0-9a-f]{40}$")
SHA40_RE = re.compile(r"[0-9a-f]{40}")
DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)


def _manifest_specs(manifest: dict) -> list[tuple[str, str]]:
    """Collect every ``@infra/ui`` spec declared in a package.json document.

    Args:
        manifest: The parsed package.json object.

    Returns:
        ``(section, spec)`` pairs for each dependency section that declares
        ``@infra/ui``.
    """
    found: list[tuple[str, str]] = []
    for section in DEPENDENCY_SECTIONS:
        deps = manifest.get(section)
        if isinstance(deps, dict) and "@infra/ui" in deps:
            found.append((section, str(deps["@infra/ui"])))
    return found


def _lockfile_violations(path: Path) -> list[tuple[int, str]]:
    """Find infra-ui references without a 40-hex revision in a lockfile.

    Args:
        path: The ``pnpm-lock.yaml`` to scan.

    Returns:
        ``(line number, stripped line)`` pairs for every line that mentions
        ``nos-tromo/infra-ui`` but carries no full commit SHA. Line numbers
        are 1-based.
    """
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if "nos-tromo/infra-ui" in line and not SHA40_RE.search(line):
            found.append((lineno, line.strip()))
    return found


def main() -> int:
    """Run the pin check and return a shell exit code.

    Scans ``<consumer-root>/<frontend-dir>/package.json`` and, when present,
    the sibling ``pnpm-lock.yaml``. A consumer with no frontend manifest or
    no ``@infra/ui`` dependency is skipped (see the module docstring).

    Returns:
        ``0`` when every ``@infra/ui`` reference is pinned to a full commit
        SHA, or the check is skipped; ``1`` on violations.
    """
    p = argparse.ArgumentParser(
        description="Validate that @infra/ui is pinned to a full-commit-SHA codeload URL.",
    )
    p.add_argument("--consumer-root", type=Path, default=Path())
    p.add_argument("--frontend-dir", default="frontend")
    args = p.parse_args()

    frontend = args.consumer_root / args.frontend_dir
    manifest_path = frontend / "package.json"
    if not manifest_path.is_file():
        print(f"no {args.frontend_dir}/package.json; skipping (no frontend).")
        return 0

    manifest = json.loads(manifest_path.read_text())
    specs = _manifest_specs(manifest)
    if not specs:
        print("package.json does not depend on @infra/ui; skipping.")
        return 0

    failures: list[str] = []
    for section, spec in specs:
        if not PINNED_URL_RE.match(spec):
            failures.append(f"{manifest_path}: {section}: unpinned @infra/ui spec: {spec}")

    lockfile_path = frontend / "pnpm-lock.yaml"
    if lockfile_path.is_file():
        for lineno, line in _lockfile_violations(lockfile_path):
            failures.append(f"{lockfile_path}:{lineno}: unpinned infra-ui ref: {line}")

    if not failures:
        print("@infra/ui pin check OK.")
        return 0

    print("@infra/ui pin check FAILED - references not pinned to a commit SHA.\n", file=sys.stderr)
    for failure in failures:
        print(failure, file=sys.stderr)
    print(
        "\nTo fix: pin the codeload tarball of the commit the release tag points to\n"
        '  "@infra/ui": "https://codeload.github.com/nos-tromo/infra-ui/tar.gz/<full-sha>"\n'
        "then refresh the lockfile with `pnpm install --no-frozen-lockfile`.\n"
        "Resolve a tag's SHA with: git ls-remote https://github.com/nos-tromo/infra-ui refs/tags/vX.Y.Z",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
