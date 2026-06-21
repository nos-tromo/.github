#!/usr/bin/env python3
"""Validate that a consumer repo's vendored make/common.mk matches the canonical copy.

Run from a consumer repo's root (or pass --consumer-root). Compares the
consumer's ``make/common.mk`` byte-for-byte against the canonical
``configs/make-common/common.mk`` in this repo. Unlike the python-strict
config (which is merged into each consumer's pyproject and so compared
semantically), common.mk is vendored verbatim, so the check is an exact
file comparison.

Adoption is **include-driven**: a repo opts in by adding
``include make/common.mk`` to its ``Makefile``.

* vendored + included    -> drift-checked (must match canonical).
* missing + included     -> failure (the Makefile references a file it did
  not vendor; ``make`` itself would also break).
* missing + not included -> skipped (the repo runs a bespoke Makefile and
  has legitimately not adopted common.mk, e.g. data-plane, open-webui).

Tying the requirement to the opt-in keeps it self-maintaining: a repo that
adopts common.mk later automatically becomes subject to the check, with no
exemption list to curate.

Exit 0 on alignment (or skip), 1 on drift or a missing-but-included file.
Stdlib-only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

# A repo opts into common.mk with an `include make/common.mk` directive in its
# Makefile; that opt-in is what makes the vendored copy required.
_INCLUDE_RE = re.compile(r"^\s*-?include\s+make/common\.mk\b", re.MULTILINE)


def _makefile_opts_in(consumer_root: Path) -> bool:
    """Return whether the consumer's Makefile opts into common.mk.

    A repo opts in by adding an ``include make/common.mk`` directive to its
    top-level ``Makefile``. That opt-in is what makes the vendored copy
    *required*: a repo with no such directive runs a bespoke Makefile and is
    legitimately exempt (e.g. data-plane, open-webui).

    Args:
        consumer_root: The consumer repo root to inspect.

    Returns:
        ``True`` if ``<consumer_root>/Makefile`` exists and contains an
        ``include make/common.mk`` directive, else ``False``.
    """
    makefile = consumer_root / "Makefile"
    if not makefile.is_file():
        return False
    return bool(_INCLUDE_RE.search(makefile.read_text()))


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
    script). A missing vendored file fails only when the consumer's Makefile
    opts in via ``include make/common.mk`` (see :func:`_makefile_opts_in`);
    otherwise it is skipped.

    Returns:
        ``0`` if the vendored copy is byte-identical to canonical (or absent
        and not adopted); ``1`` on drift, a missing-but-included file, or a
        missing canonical file.
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
        if _makefile_opts_in(args.consumer_root):
            print(
                f"error: {args.consumer_root / 'Makefile'} has `include make/common.mk` "
                f"but {consumer_file} is not vendored.",
                file=sys.stderr,
            )
            print(
                "To fix: vendor the canonical file verbatim, e.g.\n"
                "  cp <nos-tromo/.github>/configs/make-common/common.mk make/common.mk",
                file=sys.stderr,
            )
            return 1
        print("make/common.mk not vendored and not included by the Makefile; skipping (not adopted).")
        return 0

    canonical_text = canonical_file.read_text()
    consumer_text = consumer_file.read_text()
    if consumer_text == canonical_text:
        print("make/common.mk alignment check OK.")
        return 0

    print(
        "make/common.mk alignment check FAILED - vendored copy drifted from canonical.\n",
        file=sys.stderr,
    )
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
