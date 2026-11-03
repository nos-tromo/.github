"""Tests for scripts/validate_audit_lib.py beyond the self-ci fixture smoke."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "validate_audit_lib.py"
CANONICAL = ROOT / "configs" / "audit" / "audit_event.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_audit_lib", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


val = _load()


def _run(root: Path) -> int:
    return subprocess.run([sys.executable, str(SCRIPT), "--consumer-root", str(root)], check=False).returncode


def test_copies_in_skipped_directories_are_ignored(tmp_path: Path) -> None:
    for skipped in (".venv/lib", "node_modules/pkg", ".nos-tromo-github-ref/configs/audit", "pkg/__pycache__"):
        directory = tmp_path / skipped
        directory.mkdir(parents=True)
        (directory / "audit_event.py").write_text("stale\n", encoding="utf-8")
    assert val.find_vendored_copies(tmp_path) == []
    assert _run(tmp_path) == 0


def test_every_copy_is_checked(tmp_path: Path) -> None:
    for package in ("app_one/utils", "app_two"):
        (tmp_path / package).mkdir(parents=True)
        shutil.copyfile(CANONICAL, tmp_path / package / "audit_event.py")
    assert len(val.find_vendored_copies(tmp_path)) == 2
    assert _run(tmp_path) == 0
    (tmp_path / "app_two" / "audit_event.py").write_text("drifted\n", encoding="utf-8")
    assert _run(tmp_path) == 1


def test_a_missing_canonical_file_fails(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--consumer-root", str(tmp_path), "--canonical-dir", str(tmp_path / "none")],
        check=False,
    )
    assert result.returncode == 1


def test_the_aligned_fixture_mirrors_canonical() -> None:
    fixture = ROOT / "tests" / "fixtures" / "audit-aligned" / "examplepkg" / "utils" / "audit_event.py"
    assert fixture.read_bytes() == CANONICAL.read_bytes()
