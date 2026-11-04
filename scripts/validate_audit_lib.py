#!/usr/bin/env python3
"""Validate that every vendored audit library file matches its canonical copy.

Run from a consumer repo's root (or pass --consumer-root). Finds every file
named ``audit_event.py`` or ``audit_runtime.py`` in the consumer and compares
each byte-for-byte against the file of the same name in this repo's
``configs/audit/``. ``audit_event.py`` writes and verifies the federation's
hash-chained audit stream, so two copies that disagree on canonical JSON or
hashing would make one service's chain unverifiable by another's verifier.
``audit_runtime.py`` is what every emitting app must do the same way around
it: where the lines go and how a request states its purpose. Both are
vendored verbatim; the check is an exact file comparison.

Adoption is presence-driven: a repo opts in by vendoring a file, wherever its
package layout puts it (``chorus/utils/audit_event.py``,
``translator/audit_runtime.py``, ...). Unlike ``common.mk`` there is no
separate opt-in to miss: code that imports a module fails on its own without
it.

* one or more copies found -> each must match its canonical file.
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

MODULE_NAMES = ("audit_event.py", "audit_runtime.py")

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
    """Return every vendored audit library file under the consumer root.

    Args:
        consumer_root: The consumer repo root to search.

    Returns:
        The paths of every file named in ``MODULE_NAMES``, sorted, excluding
        the directories in ``_SKIP_DIRS``.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(consumer_root):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
        found.extend(Path(dirpath) / name for name in MODULE_NAMES if name in filenames)
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
            fromfile=f"canonical configs/audit/{consumer_path.name}",
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
        description="Validate every vendored audit library file matches canonical.",
    )
    p.add_argument("--consumer-root", type=Path, default=Path())
    p.add_argument("--canonical-dir", type=Path, default=default_canonical)
    args = p.parse_args()

    canonical_texts: dict[str, str] = {}
    for name in MODULE_NAMES:
        canonical_file = args.canonical_dir / name
        if not canonical_file.is_file():
            print(f"error: canonical file missing at {canonical_file}", file=sys.stderr)
            return 1
        canonical_texts[name] = canonical_file.read_text(encoding="utf-8")

    copies = find_vendored_copies(args.consumer_root)
    if not copies:
        print(f"{' / '.join(MODULE_NAMES)} not vendored; skipping (not adopted).")
        return 0

    drifted = 0
    for copy in copies:
        canonical_text = canonical_texts[copy.name]
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
            "  cp <nos-tromo/.github>/configs/audit/<file name> <path of the drifted copy>",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
