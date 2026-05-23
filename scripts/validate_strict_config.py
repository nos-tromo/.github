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
    """Coerce a configparser string value to a Python primitive.

    configparser returns every value as a string; this normalizes the
    canonical mypy.ini values so they compare equal to the real
    bools/ints loaded from the consumer's pyproject TOML.

    Args:
        raw: The raw string as read from configparser.

    Returns:
        ``True``/``False`` for the literal strings (case-insensitive),
        an ``int`` for any integer literal (including negative),
        otherwise the stripped string.
    """
    stripped = raw.strip()
    low = stripped.lower()
    if low in ("true", "false"):
        return low == "true"
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict into single-level dotted keys.

    Renders TOML table paths (``tool.ruff.lint.select``) as flat keys
    so canonical and consumer configs can be diffed key-for-key.

    Args:
        d: The nested dict to flatten.
        prefix: Internal recursion accumulator; callers should leave
            this at the default.

    Returns:
        A new dict whose keys are dot-joined paths to every leaf value
        in ``d``.
    """
    flat: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        else:
            flat[key] = v
    return flat


def _load_canonical_ruff(path: Path) -> dict[str, Any]:
    """Parse the canonical ``ruff.toml`` into flattened-key form.

    Args:
        path: Filesystem path to the canonical ``ruff.toml``.

    Returns:
        The TOML contents as a single-level dict keyed by dotted
        table paths (see :func:`_flatten`).
    """
    return _flatten(tomllib.loads(path.read_text()))


def _load_canonical_mypy(path: Path) -> dict[str, Any]:
    """Parse the canonical ``mypy.ini`` ``[mypy]`` section into a typed dict.

    Args:
        path: Filesystem path to the canonical ``mypy.ini``.

    Returns:
        A dict mapping each option name in the ``[mypy]`` section to a
        Python-typed value (bool/int/str — see :func:`_coerce_ini_value`).
    """
    parser = configparser.ConfigParser()
    parser.read_string(path.read_text())
    section = parser["mypy"]
    return {k: _coerce_ini_value(section[k]) for k in section}


def _load_canonical_versions(path: Path) -> dict[str, str]:
    """Parse the ``tool: rev`` lines in ``precommit-versions.yaml``.

    Hand-rolled rather than using a YAML library so this script stays
    stdlib-only; the file is a flat ``key: value`` mapping with ``#``
    comments.

    Args:
        path: Filesystem path to ``precommit-versions.yaml``.

    Returns:
        A dict mapping each tool name to its pinned rev (e.g.
        ``{"ruff": "v0.15.14"}``).
    """
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip().strip("\"'")
    return out


def _extract_precommit_rev(text: str, repo_url: str) -> str | None:
    """Return the ``rev:`` pinned for ``repo_url`` in a pre-commit config.

    Uses a regex rather than a YAML parser to keep this script
    stdlib-only; matches the standard layout of ``- repo: <url>``
    followed by ``rev: <value>`` on a subsequent line, with optional
    quoting.

    Args:
        text: Full contents of ``.pre-commit-config.yaml``.
        repo_url: The ``repo:`` URL to look up.

    Returns:
        The rev string (unquoted), or ``None`` if no matching hook
        block is found.
    """
    pattern = re.compile(
        r"-\s*repo:\s*" + re.escape(repo_url) + r"[^\n]*\n[^\n]*?rev:\s*[\"']?([^\"'\s]+)[\"']?",
        re.MULTILINE,
    )
    m = pattern.search(text)
    return m.group(1) if m else None


def _diff(label: str, consumer: dict[str, Any], canonical: dict[str, Any], overrides: set[str]) -> list[str]:
    """Compare a consumer config dict to the canonical and report drift.

    Reports three kinds of drift: missing canonical keys, value
    mismatches, and extra keys the consumer added beyond the canonical
    set. Keys named in ``overrides`` are skipped in every direction.

    Args:
        label: Tag inserted into each drift message (e.g.
            ``"tool.ruff"``) so readers can locate the offending block
            in their ``pyproject.toml``.
        consumer: The consumer's flattened config dict.
        canonical: The canonical flattened config dict.
        overrides: Keys the consumer is permitted to set freely (e.g.
            :data:`ALLOWED_RUFF_OVERRIDES`).

    Returns:
        A list of human-readable drift messages, one per discrepancy.
        Empty list means the consumer is aligned.
    """
    drifts: list[str] = []
    for key, expected in canonical.items():
        if key in overrides:
            continue
        if key not in consumer:
            drifts.append(f"  [{label}] missing: {key} (canonical: {expected!r})")
            continue
        if consumer[key] != expected:
            drifts.append(
                f"  [{label}] {key} drifted:\n      consumer:  {consumer[key]!r}\n      canonical: {expected!r}"
            )
    for key, value in consumer.items():
        if key in overrides or key in canonical:
            continue
        drifts.append(f"  [{label}] extra key not in canonical: {key} = {value!r}")
    return drifts


def main() -> int:
    """Run the alignment check and return a shell exit code.

    Reads the consumer's ``pyproject.toml`` and
    ``.pre-commit-config.yaml`` (rooted at ``--consumer-root``,
    default ``.``) and diffs them against the canonical configs in
    ``--canonical-dir`` (default ``configs/python-strict/`` adjacent
    to this script). Drift is printed to stderr; an OK summary is
    printed to stdout.

    Returns:
        ``0`` if the consumer is fully aligned with the canonical
        strict config, ``1`` on any drift or if a required file is
        missing.
    """
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
            drifts.append(f"  [.pre-commit-config.yaml] {repo_url} hook not found (expected rev: {expected})")
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
