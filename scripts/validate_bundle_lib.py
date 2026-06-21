#!/usr/bin/env python3
"""Validate that a consumer repo's vendored scripts/bundle-lib.sh matches canonical.

Run from a consumer repo's root (or pass --consumer-root). Compares the
consumer's ``scripts/bundle-lib.sh`` byte-for-byte against the canonical
``configs/bundle/bundle-lib.sh`` in this repo. Like make/common.mk, the
library is vendored verbatim, so the check is an exact file comparison.

Adoption is **include-driven**: a repo opts in by shipping a
``scripts/bundle_images.sh`` wrapper that sources the vendored library
(``. scripts/bundle-lib.sh``).

* vendored + sourced   -> drift-checked (must match canonical).
* missing + sourced    -> failure (the wrapper sources a file it did not
  vendor; ``make bundle`` would break).
* missing + no wrapper -> skipped (the repo has no airgap bundle flow and
  has legitimately not adopted the library, e.g. open-webui, deploy).

Tying the requirement to the opt-in keeps it self-maintaining: a repo that
adds a bundle wrapper later automatically becomes subject to the check.

Exit 0 on alignment (or skip), 1 on drift or a missing-but-sourced file.
Stdlib-only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path


def _bundle_wrapper_opts_in(consumer_root: Path) -> bool:
    """Return whether the consumer's bundle wrapper sources bundle-lib.sh.

    A repo opts in by shipping ``scripts/bundle_images.sh`` that sources the
    vendored ``scripts/bundle-lib.sh``. That opt-in is what makes the vendored
    library *required*: a repo with no such wrapper has no airgap bundle flow
    and is legitimately exempt (e.g. open-webui, deploy).

    Args:
        consumer_root: The consumer repo root to inspect.

    Returns:
        ``True`` if ``<consumer_root>/scripts/bundle_images.sh`` exists and
        references ``bundle-lib.sh``, else ``False``.
    """
    wrapper = consumer_root / "scripts" / "bundle_images.sh"
    if not wrapper.is_file():
        return False
    return "bundle-lib.sh" in wrapper.read_text()


def _unified_diff(consumer_text: str, canonical_text: str, consumer_path: Path) -> list[str]:
    """Build a unified diff between the vendored and canonical bundle-lib.sh.

    Args:
        consumer_text: Contents of the consumer's vendored ``scripts/bundle-lib.sh``.
        canonical_text: Contents of the canonical ``bundle-lib.sh``.
        consumer_path: Path used as the "to" label in the diff output.

    Returns:
        The unified-diff lines (empty when the two texts are identical).
    """
    return list(
        difflib.unified_diff(
            canonical_text.splitlines(keepends=True),
            consumer_text.splitlines(keepends=True),
            fromfile="canonical configs/bundle/bundle-lib.sh",
            tofile=str(consumer_path),
        )
    )


def main() -> int:
    """Run the alignment check and return a shell exit code.

    Reads the consumer's ``scripts/bundle-lib.sh`` (rooted at
    ``--consumer-root``, default ``.``) and compares it to the canonical
    ``bundle-lib.sh`` in ``--canonical-dir`` (default ``configs/bundle/``
    adjacent to this script). A missing vendored file fails only when the
    consumer ships a ``scripts/bundle_images.sh`` wrapper that sources it
    (see :func:`_bundle_wrapper_opts_in`); otherwise it is skipped.

    Returns:
        ``0`` if the vendored copy is byte-identical to canonical (or absent
        and not adopted); ``1`` on drift, a missing-but-sourced file, or a
        missing canonical file.
    """
    script_dir = Path(__file__).resolve().parent
    default_canonical = script_dir.parent / "configs" / "bundle"

    p = argparse.ArgumentParser(
        description="Validate a consumer's vendored scripts/bundle-lib.sh matches canonical.",
    )
    p.add_argument("--consumer-root", type=Path, default=Path())
    p.add_argument("--canonical-dir", type=Path, default=default_canonical)
    args = p.parse_args()

    canonical_file = args.canonical_dir / "bundle-lib.sh"
    consumer_file = args.consumer_root / "scripts" / "bundle-lib.sh"

    if not canonical_file.is_file():
        print(f"error: canonical file missing at {canonical_file}", file=sys.stderr)
        return 1

    if not consumer_file.is_file():
        if _bundle_wrapper_opts_in(args.consumer_root):
            print(
                f"error: {args.consumer_root / 'scripts' / 'bundle_images.sh'} sources bundle-lib.sh "
                f"but {consumer_file} is not vendored.",
                file=sys.stderr,
            )
            print(
                "To fix: vendor the canonical file verbatim, e.g.\n"
                "  cp <nos-tromo/.github>/configs/bundle/bundle-lib.sh scripts/bundle-lib.sh",
                file=sys.stderr,
            )
            return 1
        print("scripts/bundle-lib.sh not vendored and no bundle wrapper sources it; skipping (not adopted).")
        return 0

    canonical_text = canonical_file.read_text()
    consumer_text = consumer_file.read_text()
    if consumer_text == canonical_text:
        print("scripts/bundle-lib.sh alignment check OK.")
        return 0

    print(
        "scripts/bundle-lib.sh alignment check FAILED - vendored copy drifted from canonical.\n",
        file=sys.stderr,
    )
    for line in _unified_diff(consumer_text, canonical_text, consumer_file):
        sys.stderr.write(line)
    print(
        "\nTo fix: re-vendor the canonical file verbatim, e.g.\n"
        "  cp <nos-tromo/.github>/configs/bundle/bundle-lib.sh scripts/bundle-lib.sh",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
