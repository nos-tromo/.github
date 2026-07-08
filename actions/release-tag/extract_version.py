#!/usr/bin/env python3
"""Version helpers for the release-tag composite action.

Subcommands:
  extract --file <path> --source {pyproject,plain}
      Print the release version declared in <path>.
  check-increase --new <ver> --latest <ver>
      Exit 0 if <new>'s (major, minor, patch) is strictly greater than
      <latest>'s, else exit 1.

Python 3.11+ (tomllib). Standard library only.
"""
from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def extract(version_file: str, source: str) -> str:
    path = Path(version_file)
    if source == "pyproject":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        try:
            version = data["project"]["version"]
        except KeyError as exc:
            raise ValueError(f"{version_file}: no [project].version") from exc
    elif source == "plain":
        version = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                version = stripped
                break
        if not version:
            raise ValueError(f"{version_file}: file is empty")
    else:
        raise ValueError(f"unknown version-source: {source!r}")

    if not SEMVER_RE.match(version):
        raise ValueError(f"{version!r} is not a MAJOR.MINOR.PATCH version")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ex = sub.add_parser("extract")
    p_ex.add_argument("--file", required=True)
    p_ex.add_argument("--source", required=True, choices=["pyproject", "plain"])
    args = parser.parse_args(argv)

    if args.cmd == "extract":
        try:
            print(extract(args.file, args.source))
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
