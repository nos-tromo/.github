#!/usr/bin/env python3
"""Validate that a consumer repo's lint/type-check config mirrors the canonical strict config.

Run from a consumer repo's root (or pass --consumer-root). Compares the
consumer's pyproject.toml [tool.ruff] and [tool.mypy] blocks against
configs/python-strict/ruff.toml and configs/python-strict/mypy.ini, and
the consumer's .pre-commit-config.yaml hook revs against
precommit-versions.yaml.

Allowed consumer override: [tool.ruff] target-version (each repo has a
different Python floor).

Exit 0 on alignment, 1 on drift. Requires Python 3.11+ (tomllib).
"""

from __future__ import annotations

import argparse
import configparser
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


ALLOWED_RUFF_OVERRIDES = {"target-version"}
RUFF_PRECOMMIT_REPO = "https://github.com/astral-sh/ruff-pre-commit"
MYPY_PRECOMMIT_REPO = "https://github.com/pre-commit/mirrors-mypy"


def _coerce_ini_value(raw: str) -> Any:
    stripped = raw.strip()
    low = stripped.lower()
    if low in ("true", "false"):
        return low == "true"
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        else:
            flat[key] = v
    return flat


def _load_canonical_ruff(path: Path) -> dict[str, Any]:
    return _flatten(tomllib.loads(path.read_text()))


def _load_canonical_mypy(path: Path) -> dict[str, Any]:
    parser = configparser.ConfigParser()
    parser.read_string(path.read_text())
    section = parser["mypy"]
    return {k: _coerce_ini_value(section[k]) for k in section}


def _load_canonical_versions(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip("\"'")
    return out


def _extract_precommit_rev(text: str, repo_url: str) -> str | None:
    pattern = re.compile(
        r"-\s*repo:\s*" + re.escape(repo_url) + r"[^\n]*\n[^\n]*?rev:\s*[\"']?([^\"'\s]+)[\"']?",
        re.MULTILINE,
    )
    m = pattern.search(text)
    return m.group(1) if m else None


def _diff(label: str, consumer: dict[str, Any], canonical: dict[str, Any], overrides: set[str]) -> list[str]:
    drifts: list[str] = []
    for key, expected in canonical.items():
        if key in overrides:
            continue
        if key not in consumer:
            drifts.append(f"  [{label}] missing: {key} (canonical: {expected!r})")
            continue
        if consumer[key] != expected:
            drifts.append(
                f"  [{label}] {key} drifted:\n"
                f"      consumer:  {consumer[key]!r}\n"
                f"      canonical: {expected!r}"
            )
    for key, value in consumer.items():
        if key in overrides or key in canonical:
            continue
        drifts.append(f"  [{label}] extra key not in canonical: {key} = {value!r}")
    return drifts


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    default_canonical = script_dir.parent / "configs" / "python-strict"

    p = argparse.ArgumentParser(
        description="Validate consumer ruff/mypy/pre-commit alignment with canonical strict config.",
    )
    p.add_argument("--consumer-root", type=Path, default=Path("."))
    p.add_argument("--canonical-dir", type=Path, default=default_canonical)
    args = p.parse_args()

    consumer_pyproject = args.consumer_root / "pyproject.toml"
    consumer_precommit = args.consumer_root / ".pre-commit-config.yaml"
    canonical_ruff = args.canonical_dir / "ruff.toml"
    canonical_mypy = args.canonical_dir / "mypy.ini"
    canonical_versions = args.canonical_dir / "precommit-versions.yaml"

    required = {
        "consumer pyproject.toml": consumer_pyproject,
        "consumer .pre-commit-config.yaml": consumer_precommit,
        "canonical ruff.toml": canonical_ruff,
        "canonical mypy.ini": canonical_mypy,
        "canonical precommit-versions.yaml": canonical_versions,
    }
    missing = [f"{name} at {path}" for name, path in required.items() if not path.is_file()]
    if missing:
        print("error: required file(s) missing:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        return 1

    with consumer_pyproject.open("rb") as f:
        pyproject = tomllib.load(f)
    consumer_ruff = _flatten(pyproject.get("tool", {}).get("ruff", {}))
    consumer_mypy = pyproject.get("tool", {}).get("mypy", {})

    drifts: list[str] = []
    drifts += _diff("tool.ruff", consumer_ruff, _load_canonical_ruff(canonical_ruff), ALLOWED_RUFF_OVERRIDES)
    drifts += _diff("tool.mypy", consumer_mypy, _load_canonical_mypy(canonical_mypy), set())

    versions = _load_canonical_versions(canonical_versions)
    precommit_text = consumer_precommit.read_text()
    for tool, repo_url in (("ruff", RUFF_PRECOMMIT_REPO), ("mypy", MYPY_PRECOMMIT_REPO)):
        expected = versions.get(tool)
        if not expected:
            drifts.append(f"  [precommit-versions.yaml] canonical missing entry: {tool}")
            continue
        actual = _extract_precommit_rev(precommit_text, repo_url)
        if actual is None:
            drifts.append(
                f"  [.pre-commit-config.yaml] {repo_url} hook not found "
                f"(expected rev: {expected})"
            )
        elif actual != expected:
            drifts.append(
                f"  [.pre-commit-config.yaml] {repo_url} rev drifted:\n"
                f"      consumer:  {actual}\n"
                f"      canonical: {expected}"
            )

    if drifts:
        print("Strict-config alignment check FAILED.\n", file=sys.stderr)
        for d in drifts:
            print(d, file=sys.stderr)
        print(
            "\nTo fix: mirror nos-tromo/.github/configs/python-strict/ contents into\n"
            "  - your pyproject.toml ([tool.ruff], [tool.mypy])\n"
            "  - your .pre-commit-config.yaml (ruff and mypy hook revs)",
            file=sys.stderr,
        )
        return 1

    print("Strict-config alignment check OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
