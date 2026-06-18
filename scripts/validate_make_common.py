#!/usr/bin/env python3
"""Validate that a consumer repo's vendored make/common.mk matches the canonical copy.

Run from a consumer repo's root (or pass --consumer-root). Compares the
consumer's ``make/common.mk`` byte-for-byte against the canonical
``configs/make-common/common.mk`` in this repo. Unlike the python-strict
config (which is merged into each consumer's pyproject and so compared
semantically), common.mk is vendored verbatim, so the check is an exact
file comparison.

Consumers that have not yet adopted common.mk (no ``make/common.mk``
present) are skipped, so the check can ride in CI during an incremental
rollout without failing repos that have not migrated. Flip
``REQUIRE_PRESENT`` to ``True`` once every repo has vendored the file to
turn absence into a failure.

Exit 0 on alignment (or skip), 1 on drift. Stdlib-only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

# Flip to True once every consumer repo vendors make/common.mk (rollout done),
# so a missing vendored copy becomes a failure instead of a skip.
REQUIRE_PRESENT = False


def _unified_diff(consumer_text: str, canonical_text: str, consumer_path: Path) -> list[str]:
    """Build a unified diff between the vendored and canonical common.mk.

    Args:
        consumer_text: Contents of the consumer's vendored ``make/common.mk``.
        canonical_text: Contents of the canonical ``common.mk``.
        consumer_path: Path used as the "to" label in the diff output.

    Returns:
        The unified-diff lines (empty when the two texts are identical).
    """
    return list(
        difflib.unified_diff(
            canonical_text.splitlines(keepends=True),
            consumer_text.splitlines(keepends=True),
            fromfile="canonical configs/make-common/common.mk",
            tofile=str(consumer_path),
        )
    )


def main() -> int:
    """Run the alignment check and return a shell exit code.

    Reads the consumer's ``make/common.mk`` (rooted at ``--consumer-root``,
    default ``.``) and compares it to the canonical ``common.mk`` in
    ``--canonical-dir`` (default ``configs/make-common/`` adjacent to this
    script). A missing vendored file is skipped unless ``REQUIRE_PRESENT``
    is set.

    Returns:
        ``0`` if the vendored copy is byte-identical to canonical (or absent
        and not required); ``1`` on drift or a missing canonical file.
    """
    script_dir = Path(__file__).resolve().parent
    default_canonical = script_dir.parent / "configs" / "make-common"

    p = argparse.ArgumentParser(
        description="Validate a consumer's vendored make/common.mk matches canonical.",
    )
    p.add_argument("--consumer-root", type=Path, default=Path())
    p.add_argument("--canonical-dir", type=Path, default=default_canonical)
    args = p.parse_args()

    canonical_file = args.canonical_dir / "common.mk"
    consumer_file = args.consumer_root / "make" / "common.mk"

    if not canonical_file.is_file():
        print(f"error: canonical file missing at {canonical_file}", file=sys.stderr)
        return 1

    if not consumer_file.is_file():
        if REQUIRE_PRESENT:
            print(f"error: {consumer_file} missing (make/common.mk is required)", file=sys.stderr)
            return 1
        print(f"make/common.mk not vendored at {consumer_file}; skipping (not yet migrated).")
        return 0

    canonical_text = canonical_file.read_text()
    consumer_text = consumer_file.read_text()
    if consumer_text == canonical_text:
        print("make/common.mk alignment check OK.")
        return 0

    print("make/common.mk alignment check FAILED - vendored copy drifted from canonical.\n", file=sys.stderr)
    for line in _unified_diff(consumer_text, canonical_text, consumer_file):
        sys.stderr.write(line)
    print(
        "\nTo fix: re-vendor the canonical file verbatim, e.g.\n"
        "  cp <nos-tromo/.github>/configs/make-common/common.mk make/common.mk",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
