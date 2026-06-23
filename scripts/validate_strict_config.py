#!/usr/bin/env python3
"""Validate that a consumer repo's lint/type-check config mirrors the canonical strict config.

Run from a consumer repo's root (or pass --consumer-root). Compares the
consumer's pyproject.toml [tool.ruff] and [tool.pyrefly] blocks against
configs/python-strict/ruff.toml and configs/python-strict/pyrefly.toml, and
the consumer's .pre-commit-config.yaml hook revs against
precommit-versions.yaml.

Allowed consumer override: [tool.ruff] target-version (each repo has a
different Python floor).

Exit 0 on alignment, 1 on drift. Requires Python 3.11+ (tomllib).
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ALLOWED_RUFF_OVERRIDES = {"target-version"}
RUFF_PRECOMMIT_REPO = "https://github.com/astral-sh/ruff-pre-commit"


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


def _load_canonical_pyrefly(path: Path) -> dict[str, Any]:
    """Parse the canonical ``pyrefly.toml`` into flattened-key form.

    Args:
        path: Filesystem path to the canonical ``pyrefly.toml``.

    Returns:
        The TOML contents as a single-level dict keyed by dotted table
        paths (see :func:`_flatten`), e.g. ``errors.redundant-cast``.
    """
    return _flatten(tomllib.loads(path.read_text()))


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
        description="Validate consumer ruff/pyrefly/pre-commit alignment with canonical strict config.",
    )
    p.add_argument("--consumer-root", type=Path, default=Path("."))
    p.add_argument("--canonical-dir", type=Path, default=default_canonical)
    args = p.parse_args()

    consumer_pyproject = args.consumer_root / "pyproject.toml"
    consumer_precommit = args.consumer_root / ".pre-commit-config.yaml"
    canonical_ruff = args.canonical_dir / "ruff.toml"
    canonical_pyrefly = args.canonical_dir / "pyrefly.toml"
    canonical_versions = args.canonical_dir / "precommit-versions.yaml"

    required = {
        "consumer pyproject.toml": consumer_pyproject,
        "consumer .pre-commit-config.yaml": consumer_precommit,
        "canonical ruff.toml": canonical_ruff,
        "canonical pyrefly.toml": canonical_pyrefly,
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
    consumer_pyrefly = _flatten(pyproject.get("tool", {}).get("pyrefly", {}))

    drifts: list[str] = []
    drifts += _diff("tool.ruff", consumer_ruff, _load_canonical_ruff(canonical_ruff), ALLOWED_RUFF_OVERRIDES)
    drifts += _diff("tool.pyrefly", consumer_pyrefly, _load_canonical_pyrefly(canonical_pyrefly), set())

    versions = _load_canonical_versions(canonical_versions)
    precommit_text = consumer_precommit.read_text()

    # ruff still pinned by hook rev — straightforward mirrors-pre-commit usage.
    expected_ruff = versions.get("ruff")
    if not expected_ruff:
        drifts.append("  [precommit-versions.yaml] canonical missing entry: ruff")
    else:
        actual_ruff = _extract_precommit_rev(precommit_text, RUFF_PRECOMMIT_REPO)
        if actual_ruff is None:
            drifts.append(
                f"  [.pre-commit-config.yaml] {RUFF_PRECOMMIT_REPO} hook not found (expected rev: {expected_ruff})"
            )
        elif actual_ruff != expected_ruff:
            drifts.append(
                f"  [.pre-commit-config.yaml] {RUFF_PRECOMMIT_REPO} rev drifted:\n"
                f"      consumer:  {actual_ruff}\n"
                f"      canonical: {expected_ruff}"
            )

    # pyrefly is a *local* pre-commit hook running `uv run pyrefly check` so it
    # sees the project venv (resolving FastAPI/pydantic/etc. types needs the
    # installed deps). The pyrefly version itself is pinned in dev deps.
    expected_pyrefly = versions.get("pyrefly")
    if not expected_pyrefly:
        drifts.append("  [precommit-versions.yaml] canonical missing entry: pyrefly")
    else:
        if "uv run pyrefly check" not in precommit_text:
            drifts.append(
                "  [.pre-commit-config.yaml] pyrefly hook must be a `local` hook with "
                "`entry: uv run pyrefly check` (so pyrefly sees the project venv)"
            )
        dev_deps = (
            pyproject.get("dependency-groups", {}).get("dev", [])
            if isinstance(pyproject.get("dependency-groups"), dict)
            else []
        )
        expected_pin = f"pyrefly=={expected_pyrefly}"
        if expected_pin not in dev_deps:
            drifts.append(
                f"  [pyproject.toml dependency-groups.dev] must pin {expected_pin!r} "
                f"(found: {[d for d in dev_deps if isinstance(d, str) and d.startswith('pyrefly')]})"
            )

        # mypy must be fully retired. The validator no longer reads [tool.mypy],
        # so without these guards a half-migration (pyrefly added but mypy left
        # behind) would silently pass with two type-checkers and two configs.
        if "mypy" in pyproject.get("tool", {}):
            drifts.append("  [pyproject.toml] leftover [tool.mypy] block — remove it (pyrefly replaces mypy)")
        stale_mypy = [
            d for d in dev_deps if isinstance(d, str) and re.match(r"\s*mypy\s*([=<>!~;\[]|$)", d, re.IGNORECASE)
        ]
        if stale_mypy:
            drifts.append(
                f"  [pyproject.toml dependency-groups.dev] leftover mypy dependency {stale_mypy} — "
                "remove it (replaced by pyrefly)"
            )
        if "uv run mypy" in precommit_text or "mirrors-mypy" in precommit_text:
            drifts.append("  [.pre-commit-config.yaml] leftover mypy hook — remove it (replaced by the pyrefly hook)")

    if drifts:
        print("Strict-config alignment check FAILED.\n", file=sys.stderr)
        for d in drifts:
            print(d, file=sys.stderr)
        print(
            "\nTo fix: mirror nos-tromo/.github/configs/python-strict/ contents into\n"
            "  - your pyproject.toml ([tool.ruff], [tool.pyrefly], dev pyrefly pin)\n"
            "  - your .pre-commit-config.yaml (ruff rev + local pyrefly hook running `uv run pyrefly check`)\n"
            "  - and remove all mypy remnants ([tool.mypy], the mypy dev dep, the mypy hook)",
            file=sys.stderr,
        )
        return 1

    print("Strict-config alignment check OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
