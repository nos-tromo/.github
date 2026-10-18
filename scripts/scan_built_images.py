#!/usr/bin/env python3
"""Scan the images a consumer's ``docker compose build`` produced with Grype.

Run from the consumer root after the build, with the resolved compose
config (``docker compose ... config --format json``) and the path of a
Grype binary. Every service with a ``build:`` section is scanned once from
the local image store (``docker:<image>``); pulled-only services are out of
scope. Grype runs in the current directory, so a consumer's ``.grype.yaml``
applies: its ``ignore:`` rules (each with a ``reason``) are the escape hatch
for an accepted finding, and ignored matches are counted in the summary.

The gate fails only on *fixable* findings - a match whose ``fix.state`` is
``fixed``, so a newer package version exists - at or above ``--fail-on``
(default ``critical``). An unfixed vulnerability is reported but never
blocks: there is nothing to bump to yet.

Writes a markdown summary to ``$GITHUB_STEP_SUMMARY`` when that variable is
set (per-image counts with fixable counts in parentheses, then the fixable
findings down to ``high`` or the gate, whichever is lower) and ``::error``
annotations when running on GitHub Actions.

Exit 0 when the gate passes (or nothing was built), 1 when a fixable finding
reaches the gate, 2 when the scan could not run (Grype failed or printed
something that is not a report). Stdlib-only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SEVERITIES = ("critical", "high", "medium", "low", "negligible", "unknown")
RANK = {name: len(SEVERITIES) - index for index, name in enumerate(SEVERITIES)}
THRESHOLDS = ("none", "negligible", "low", "medium", "high", "critical")
MAX_ROWS = 50

Runner = Callable[..., subprocess.CompletedProcess[str]]


class ScanError(Exception):
    """Grype could not produce a report for an image."""


@dataclass(frozen=True)
class Finding:
    """One vulnerability match in one package."""

    id: str
    severity: str
    package: str
    version: str
    fixed_in: str
    type: str
    fixable: bool


@dataclass
class ImageScan:
    """The summarized Grype report for one image."""

    image: str
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SEVERITIES, 0))
    fixable: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SEVERITIES, 0))
    ignored: int = 0
    findings: list[Finding] = field(default_factory=list)


def built_images(config: dict) -> list[str]:
    """List the images compose builds, in service order, each once.

    Args:
        config: The parsed ``docker compose config --format json`` output.

    Returns:
        The ``image:`` of every service with a ``build`` section, falling
        back to compose's default ``<project>-<service>`` name.
    """
    project = config.get("name", "")
    images: list[str] = []
    for service, spec in (config.get("services") or {}).items():
        if not isinstance(spec, dict) or "build" not in spec:
            continue
        image = spec.get("image") or f"{project}-{service}"
        if image not in images:
            images.append(image)
    return images


def _severity(raw: object) -> str:
    """Normalize a Grype severity to one of :data:`SEVERITIES`."""
    value = str(raw or "").lower()
    return value if value in RANK else "unknown"


def summarize(image: str, report: dict) -> ImageScan:
    """Summarize one Grype JSON report.

    Args:
        image: The scanned image, for display.
        report: The parsed ``grype -o json`` output.

    Returns:
        Severity counts, fixable counts, the ignored-match count, and every
        finding with its package details.
    """
    scan = ImageScan(image)
    for match in report.get("matches") or []:
        vulnerability = match.get("vulnerability") or {}
        artifact = match.get("artifact") or {}
        fix = vulnerability.get("fix") or {}
        severity = _severity(vulnerability.get("severity"))
        fixable = fix.get("state") == "fixed"
        scan.counts[severity] += 1
        if fixable:
            scan.fixable[severity] += 1
        scan.findings.append(
            Finding(
                id=str(vulnerability.get("id", "")),
                severity=severity,
                package=str(artifact.get("name", "")),
                version=str(artifact.get("version", "")),
                fixed_in=", ".join(fix.get("versions") or []),
                type=str(artifact.get("type", "")),
                fixable=fixable,
            )
        )
    scan.ignored = len(report.get("ignoredMatches") or [])
    return scan


def _at_least(severity: str, threshold: str) -> bool:
    """Return whether ``severity`` is at or above a gate threshold."""
    return threshold != "none" and RANK[severity] >= RANK[threshold]


def gate(scan: ImageScan, fail_on: str) -> list[Finding]:
    """Return the findings that fail the gate.

    Args:
        scan: One summarized image.
        fail_on: A value from :data:`THRESHOLDS`.

    Returns:
        Fixable findings at or above ``fail_on``, most severe first; empty
        for ``none``.
    """
    hits = [f for f in scan.findings if f.fixable and _at_least(f.severity, fail_on)]
    return sorted(hits, key=lambda f: (-RANK[f.severity], f.id))


def scan_image(grype: str, image: str, runner: Runner = subprocess.run) -> dict:
    """Run Grype against one locally built image.

    Args:
        grype: Path to the Grype executable.
        image: The image as named in the local image store.
        runner: ``subprocess.run`` or a test double with the same signature.

    Returns:
        The parsed JSON report.

    Raises:
        ScanError: Grype is missing, failed, or printed no JSON report.
    """
    cmd = [grype, f"docker:{image}", "-o", "json"]
    env = {**os.environ, "GRYPE_CHECK_FOR_APP_UPDATE": "false"}
    try:
        proc = runner(cmd, capture_output=True, text=True, env=env, check=False)
    except FileNotFoundError as exc:
        raise ScanError(f"grype not found at {grype}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ScanError(f"grype exited {proc.returncode} on {image}: {detail[-1] if detail else 'no output'}")
    try:
        report = json.loads(proc.stdout)
    except ValueError as exc:
        raise ScanError(f"grype printed no JSON report for {image}") from exc
    if not isinstance(report, dict):
        raise ScanError(f"grype printed no JSON report for {image}")
    return report


def _cell(text: str) -> str:
    """Escape text for a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def render_summary(scans: list[ImageScan], fail_on: str, descriptor: dict | None = None) -> str:
    """Render the scan results as markdown for the GitHub job summary.

    Args:
        scans: One entry per scanned image.
        fail_on: The gate threshold.
        descriptor: Grype's ``descriptor`` block, for the version and the
            vulnerability-database build date.

    Returns:
        A markdown section.
    """
    descriptor = descriptor or {}
    version = descriptor.get("version", "?")
    db = descriptor.get("db") or {}
    built = str((db.get("status") or db).get("built", ""))[:10] or "?"
    failed = any(gate(scan, fail_on) for scan in scans)
    verdict = "**failed**" if failed else "passed"
    gate_text = "none (report only)" if fail_on == "none" else f"fixable {fail_on} or worse"

    lines = [
        "### Image vulnerability scan",
        "",
        f"Grype {version}, vulnerability DB built {built}. Gate: {gate_text} - {verdict}.",
        "Counts are matches; fixable ones (a newer package version exists) in parentheses.",
        "",
        "| Image | Critical | High | Medium | Low | Negligible | Unknown | Ignored |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for scan in scans:
        cells = [
            f"{scan.counts[s]} ({scan.fixable[s]})" if scan.fixable[s] else str(scan.counts[s]) for s in SEVERITIES
        ]
        lines.append(f"| `{scan.image}` | {' | '.join(cells)} | {scan.ignored} |")

    floor = fail_on if fail_on != "none" and RANK[fail_on] < RANK["high"] else "high"
    rows = [
        (scan.image, finding)
        for scan in scans
        for finding in sorted(scan.findings, key=lambda f: (-RANK[f.severity], f.id))
        if finding.fixable and _at_least(finding.severity, floor)
    ]
    if rows:
        lines += [
            "",
            f"Fixable findings, {floor} or worse:",
            "",
            "| Image | ID | Severity | Package | Installed | Fixed in | Type |",
            "|---|---|---|---|---|---|---|",
        ]
        for image, f in rows[:MAX_ROWS]:
            cells = [f.id, f.severity, f.package, f.version, f.fixed_in, f.type]
            lines.append(f"| `{image}` | {' | '.join(_cell(c) for c in cells)} |")
        if len(rows) > MAX_ROWS:
            lines.append(f"\n...and {len(rows) - MAX_ROWS} more; see the job log.")
    return "\n".join([*lines, ""])


def main(argv: list[str] | None = None, runner: Runner = subprocess.run) -> int:
    """Scan every built image and apply the gate.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).
        runner: ``subprocess.run`` or a test double with the same signature.

    Returns:
        ``0`` when the gate passes or nothing was built, ``1`` when a fixable
        finding reaches the gate, ``2`` when a scan could not run.
    """
    p = argparse.ArgumentParser(description="Scan the images docker compose built with Grype.")
    p.add_argument("--compose-config", type=Path, required=True, help="docker compose config --format json output")
    p.add_argument("--grype", required=True, help="path to the grype executable")
    p.add_argument("--fail-on", choices=THRESHOLDS, default="critical")
    args = p.parse_args(argv)

    images = built_images(json.loads(args.compose_config.read_text()))
    if not images:
        print("compose builds no images; nothing to scan.")
        return 0

    scans: list[ImageScan] = []
    descriptor: dict | None = None
    try:
        for image in images:
            report = scan_image(args.grype, image, runner)
            descriptor = descriptor or report.get("descriptor")
            scans.append(summarize(image, report))
    except ScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a") as handle:
            handle.write(render_summary(scans, args.fail_on, descriptor) + "\n")

    on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    failed = False
    for scan in scans:
        counts = ", ".join(f"{scan.counts[s]} {s}" for s in SEVERITIES if scan.counts[s])
        print(f"{scan.image}: {counts or 'no findings'} ({sum(scan.fixable.values())} fixable, {scan.ignored} ignored)")
        hits = gate(scan, args.fail_on)
        if not hits:
            continue
        failed = True
        listed = ", ".join(f"{f.id} ({f.package} {f.version} -> {f.fixed_in})" for f in hits[:10])
        more = f" and {len(hits) - 10} more" if len(hits) > 10 else ""
        message = f"{scan.image}: {len(hits)} fixable {args.fail_on}-or-worse finding(s): {listed}{more}"
        print(f"::error::{message}" if on_actions else f"FAIL: {message}", file=sys.stderr)

    if failed:
        print(
            "\nBump the affected package (lockfile or base-image digest), or record an accepted finding\n"
            "in the repo's .grype.yaml under `ignore:` with a `reason`.",
            file=sys.stderr,
        )
        return 1
    print(f"image scan OK ({len(scans)} image(s), gate: {args.fail_on}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
