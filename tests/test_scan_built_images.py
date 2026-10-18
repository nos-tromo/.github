"""Unit tests for scripts/scan_built_images.py.

Run with: uv run --with pytest python -m pytest tests/test_scan_built_images.py -q

The Grype reports here are synthetic (invented IDs and packages) but keep
the shape Grype's JSON output has; self-ci's scan-smoke job runs the real
scanner against a real image to keep that shape honest.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scan_built_images as sbi  # noqa: E402


def match(vuln_id: str, severity: str, state: str, package: str = "examplelib", fixed: str = "") -> dict:
    return {
        "vulnerability": {
            "id": vuln_id,
            "severity": severity,
            "fix": {"versions": [fixed] if fixed else [], "state": state},
        },
        "artifact": {"name": package, "version": "1.0.0", "type": "deb"},
    }


REPORT = {
    "descriptor": {"name": "grype", "version": "0.118.0", "db": {"status": {"built": "2026-01-02T03:04:05Z"}}},
    "matches": [
        match("CVE-0000-0001", "Critical", "fixed", "libexample", "1.0.1"),
        match("CVE-0000-0002", "Critical", "not-fixed"),
        match("CVE-0000-0003", "High", "fixed", "otherlib", "2.0.0"),
        match("CVE-0000-0004", "Medium", ""),
        match("CVE-0000-0005", "Medium", "fixed", "midlib", "3.1"),
        match("CVE-0000-0006", "Negligible", "wont-fix"),
        match("CVE-0000-0007", "Unknown", "unknown"),
    ],
    "ignoredMatches": [match("CVE-0000-0008", "Critical", "fixed"), match("CVE-0000-0009", "High", "fixed")],
}
CLEAN = {"descriptor": REPORT["descriptor"], "matches": [match("CVE-0000-0002", "Critical", "not-fixed")]}


# --- built_images ----------------------------------------------------------


def test_built_images_takes_services_with_a_build_section_only():
    config = {
        "name": "demo",
        "services": {
            "backend": {"build": {"context": "."}, "image": "demo-backend:latest"},
            "frontend": {"build": {"context": "frontend"}},
            "db": {"image": "docker.io/example/db:5"},
        },
    }
    assert sbi.built_images(config) == ["demo-backend:latest", "demo-frontend"]


def test_built_images_lists_a_shared_image_once():
    config = {
        "name": "demo",
        "services": {
            "app": {"build": {"context": "."}, "image": "demo-app:1"},
            "worker": {"build": {"context": "."}, "image": "demo-app:1"},
        },
    }
    assert sbi.built_images(config) == ["demo-app:1"]


# --- summarize -------------------------------------------------------------


def test_summarize_counts_by_severity_fixability_and_ignores():
    scan = sbi.summarize("demo-backend:latest", REPORT)
    assert scan.counts == {"critical": 2, "high": 1, "medium": 2, "low": 0, "negligible": 1, "unknown": 1}
    assert scan.fixable == {"critical": 1, "high": 1, "medium": 1, "low": 0, "negligible": 0, "unknown": 0}
    assert scan.ignored == 2


def test_summarize_keeps_package_details_of_findings():
    finding = next(f for f in sbi.summarize("x", REPORT).findings if f.id == "CVE-0000-0001")
    assert (finding.severity, finding.package, finding.version, finding.fixed_in, finding.type) == (
        "critical",
        "libexample",
        "1.0.0",
        "1.0.1",
        "deb",
    )
    assert finding.fixable


# --- gate ------------------------------------------------------------------


def test_gate_at_critical_fails_only_on_fixable_criticals():
    assert [f.id for f in sbi.gate(sbi.summarize("x", REPORT), "critical")] == ["CVE-0000-0001"]


def test_gate_at_high_includes_fixable_highs():
    assert [f.id for f in sbi.gate(sbi.summarize("x", REPORT), "high")] == ["CVE-0000-0001", "CVE-0000-0003"]


def test_gate_none_never_fails():
    assert sbi.gate(sbi.summarize("x", REPORT), "none") == []


def test_gate_ignores_an_unfixed_critical():
    assert sbi.gate(sbi.summarize("x", CLEAN), "critical") == []


# --- render_summary --------------------------------------------------------


def test_summary_has_counts_fixable_findings_and_the_gate_result():
    text = sbi.render_summary([sbi.summarize("demo-backend:latest", REPORT)], "critical", REPORT["descriptor"])
    assert "| `demo-backend:latest` | 2 (1) | 1 (1) | 2 (1) | 0 | 1 | 1 | 2 |" in text
    assert "| `demo-backend:latest` | CVE-0000-0001 | critical | libexample | 1.0.0 | 1.0.1 | deb |" in text
    assert "CVE-0000-0003" in text
    assert "CVE-0000-0005" not in text
    assert "**failed**" in text
    assert "0.118.0" in text
    assert "2026-01-02" in text


def test_summary_shows_findings_down_to_the_gate_when_it_is_below_high():
    text = sbi.render_summary([sbi.summarize("x", REPORT)], "medium", REPORT["descriptor"])
    assert "CVE-0000-0005" in text


# --- main ------------------------------------------------------------------


class FakeGrype:
    """Answers `grype docker:<image> -o json` with a canned report per image."""

    def __init__(self, reports: dict[str, object], returncode: int = 0):
        self.reports = reports
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        image = cmd[1].removeprefix("docker:")
        out = self.reports.get(image, "")
        stdout = out if isinstance(out, str) else json.dumps(out)
        stderr = "" if self.returncode == 0 else "failed to fetch image"
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=stdout, stderr=stderr)


def write_config(tmp_path: Path, services: dict) -> Path:
    path = tmp_path / "compose.json"
    path.write_text(json.dumps({"name": "demo", "services": services}))
    return path


SERVICES = {"backend": {"build": {"context": "."}, "image": "demo-backend:latest"}}


def test_main_fails_on_a_fixable_critical_and_annotates_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    argv = ["--compose-config", str(write_config(tmp_path, SERVICES)), "--grype", "grype"]
    assert sbi.main(argv, runner=FakeGrype({"demo-backend:latest": REPORT})) == 1
    err = capsys.readouterr().err
    assert "::error::demo-backend:latest" in err
    assert "CVE-0000-0001" in err


def test_main_passes_at_fail_on_none_and_writes_the_step_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    argv = ["--compose-config", str(write_config(tmp_path, SERVICES)), "--grype", "grype", "--fail-on", "none"]
    assert sbi.main(argv, runner=FakeGrype({"demo-backend:latest": REPORT})) == 0
    assert "demo-backend:latest" in summary.read_text()


def test_main_scans_images_with_the_docker_source(tmp_path):
    runner = FakeGrype({"demo-backend:latest": CLEAN})
    argv = ["--compose-config", str(write_config(tmp_path, SERVICES)), "--grype", "/opt/grype"]
    assert sbi.main(argv, runner=runner) == 0
    assert runner.calls == [["/opt/grype", "docker:demo-backend:latest", "-o", "json"]]


def test_main_returns_2_when_grype_fails(tmp_path):
    argv = ["--compose-config", str(write_config(tmp_path, SERVICES)), "--grype", "grype"]
    assert sbi.main(argv, runner=FakeGrype({}, returncode=1)) == 2


def test_main_returns_2_on_unparseable_grype_output(tmp_path):
    argv = ["--compose-config", str(write_config(tmp_path, SERVICES)), "--grype", "grype"]
    assert sbi.main(argv, runner=FakeGrype({"demo-backend:latest": "not json"})) == 2


def test_main_skips_when_compose_builds_nothing(tmp_path, capsys):
    argv = ["--compose-config", str(write_config(tmp_path, {"db": {"image": "x:1"}})), "--grype", "grype"]
    assert sbi.main(argv, runner=FakeGrype({})) == 0
    assert "nothing to scan" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["severe", "CRITICAL!"])
def test_main_rejects_an_unknown_threshold(tmp_path, bad):
    argv = ["--compose-config", str(write_config(tmp_path, SERVICES)), "--grype", "grype", "--fail-on", bad]
    with pytest.raises(SystemExit):
        sbi.main(argv, runner=FakeGrype({}))
