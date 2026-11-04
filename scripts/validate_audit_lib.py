#!/usr/bin/env python3
"""Validate that every vendored audit_event.py matches the canonical copy.

Run from a consumer repo's root (or pass --consumer-root). Finds every file
named ``audit_event.py`` in the consumer and compares each byte-for-byte
against the canonical ``configs/audit/audit_event.py`` in this repo. The
module writes and verifies the federation's hash-chained audit stream, so
two copies that disagree on canonical JSON or hashing would make one
service's chain unverifiable by another's verifier. It is vendored verbatim;
the check is an exact file comparison.

Adoption is presence-driven: a repo opts in by vendoring the file, wherever
its package layout puts it (``chorus/utils/audit_event.py``,
``translator/audit_event.py``, ...). Unlike ``common.mk`` there is no
separate opt-in to miss: code that imports the module fails on its own
without it.

* one or more copies found -> each must match canonical.
* none found               -> skipped (the repo does not emit audit events).

Virtual environments, ``node_modules``, VCS metadata and the hub checkout
CI places at ``.nos-tromo-github-ref`` are not searched.

Exit 0 on alignment (or skip), 1 on drift or a missing canonical file.
Stdlib-only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path

MODULE_NAME = "audit_event.py"

# Directory names never searched: third-party trees, caches, VCS metadata,
# and the canonical checkout CI drops into the consumer root.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".tox",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".nos-tromo-github-ref",
    }
)


def find_vendored_copies(consumer_root: Path) -> list[Path]:
    """Return every ``audit_event.py`` under the consumer root.

    Args:
        consumer_root: The consumer repo root to search.

    Returns:
        The paths found, sorted, excluding the directories in ``_SKIP_DIRS``.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(consumer_root):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
        if MODULE_NAME in filenames:
            found.append(Path(dirpath) / MODULE_NAME)
    return sorted(found)


def _unified_diff(consumer_text: str, canonical_text: str, consumer_path: Path) -> list[str]:
    """Build a unified diff between a vendored copy and the canonical module.

    Args:
        consumer_text: Contents of the vendored copy.
        canonical_text: Contents of the canonical module.
        consumer_path: Path used as the "to" label in the diff output.

    Returns:
        The unified-diff lines (empty when the two texts are identical).
    """
    return list(
        difflib.unified_diff(
            canonical_text.splitlines(keepends=True),
            consumer_text.splitlines(keepends=True),
            fromfile="canonical configs/audit/audit_event.py",
            tofile=str(consumer_path),
        )
    )


def main() -> int:
    """Run the alignment check and return a shell exit code.

    Returns:
        ``0`` if every vendored copy is byte-identical to canonical (or none
        is vendored); ``1`` on drift or a missing canonical file.
    """
    script_dir = Path(__file__).resolve().parent
    default_canonical = script_dir.parent / "configs" / "audit"

    p = argparse.ArgumentParser(
        description="Validate every vendored audit_event.py matches canonical.",
    )
    p.add_argument("--consumer-root", type=Path, default=Path())
    p.add_argument("--canonical-dir", type=Path, default=default_canonical)
    args = p.parse_args()

    canonical_file = args.canonical_dir / MODULE_NAME
    if not canonical_file.is_file():
        print(f"error: canonical file missing at {canonical_file}", file=sys.stderr)
        return 1

    copies = find_vendored_copies(args.consumer_root)
    if not copies:
        print(f"{MODULE_NAME} not vendored; skipping (not adopted).")
        return 0

    canonical_text = canonical_file.read_text(encoding="utf-8")
    drifted = 0
    for copy in copies:
        consumer_text = copy.read_text(encoding="utf-8")
        if consumer_text == canonical_text:
            print(f"{copy}: alignment check OK.")
            continue
        drifted += 1
        print(f"{copy}: alignment check FAILED - vendored copy drifted from canonical.\n", file=sys.stderr)
        for line in _unified_diff(consumer_text, canonical_text, copy):
            sys.stderr.write(line)

    if drifted:
        print(
            "\nTo fix: re-vendor the canonical file verbatim, e.g.\n"
            f"  cp <nos-tromo/.github>/configs/audit/{MODULE_NAME} <path of the drifted copy>",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
