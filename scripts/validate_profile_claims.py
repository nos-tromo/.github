#!/usr/bin/env python3
"""Validate that the public profile page's factual claims still hold.

``profile/README.md`` describes twelve sibling repositories in prose. Nothing
else in this repo can tell when one of those descriptions stops being true,
because the evidence lives in another repository entirely. This script closes
that gap: every checkable claim on the page is anchored, in
``profile/claims.toml``, to a line of *configuration or source* in the member
repo it describes.

Anchoring to config rather than prose is the whole point. A member's own docs
routinely mention a capability as planned, deprecated or hypothetical, so a
check that greps them passes claims that are false in practice. Config states
what the system actually does.

Exit codes are three-valued so a scheduled caller can tell a false claim from
a broken run:

* ``0`` - every anchored claim holds.
* ``1`` - drift: a claim no longer holds, or the page and the claims file
  disagree about which repos exist.
* ``2`` - the check could not run: unreadable claims file, a missing member
  checkout, or an unexpected exception.

Green means "every anchored fact still holds", **not** "the page is accurate".
This script cannot judge prose, and a claim nobody anchored is a claim nobody
checks.

Stdlib-only; Python 3.11+. Never performs network access: callers supply
member checkouts on disk.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NO_DIAGRAM_MARKER = "<!-- profile:no-diagram -->"


ASSERTION_KINDS = ("matches", "absent", "json_len", "glob_count", "exists")

_CLAIM_KEYS = frozenset({"id", "repo", "quote", "says", "in", "file", "raw", "allow_prose", "hits", *ASSERTION_KINDS})


class PageError(Exception):
    """The profile page could not be parsed, or is internally inconsistent."""


class ClaimsError(Exception):
    """The claims file is malformed, so no claim can be trusted."""


@dataclass(frozen=True)
class TableRow:
    """One repository row from a markdown table on the profile page."""

    slug: str
    org: str
    cell: str
    no_diagram: bool = False


_ROW_RE = re.compile(r"^\|\s*\*\*\[(?P<text>[^\]]+)\]\((?P<url>[^)\s]+?)/?\)\*\*\s*\|(?P<cell>.*)\|\s*$")


def parse_table_rows(page: str) -> list[TableRow]:
    """Extract the repository rows from the profile page's markdown tables.

    Only fully line-anchored table rows count. Bullet-list links elsewhere on
    the page (the "Other public projects" section) name repositories that are
    deliberately not federation members, so an unanchored link scan would pull
    them in.

    Args:
        page: Full text of the profile page.

    Returns:
        One entry per table row, in page order.

    Raises:
        PageError: A row's link text differs from the repository slug it
            points at, which means the visible name and the link disagree.
    """
    rows: list[TableRow] = []
    no_diagram = False
    for line in page.splitlines():
        if line.startswith("#"):
            no_diagram = False
        elif line.strip() == NO_DIAGRAM_MARKER:
            no_diagram = True
        match = _ROW_RE.match(line)
        if match is None:
            continue
        url = match["url"]
        parts = [p for p in url.split("/") if p]
        org, slug = parts[-2], parts[-1]
        text = match["text"]
        if text != slug:
            raise PageError(f"link text {text!r} does not match repo slug {slug!r}")
        rows.append(TableRow(slug=slug, org=org, cell=match["cell"], no_diagram=no_diagram))
    return rows


_MERMAID_RE = re.compile(r"^```mermaid$\n(.*?)^```$", re.MULTILINE | re.DOTALL)
_EDGE_LABEL_RE = re.compile(r"\|[^|]*\|")
_NODE_RE = re.compile(r"(?<![\w-])(?P<id>[A-Za-z_][\w-]*)\s*(?:\(\[|\[\[|\[\(|\(\(|\[|\(|\{)\s*\"(?P<label>[^\"]*)\"")
_LABEL_SEP_RE = re.compile(r"<br\s*/?>")


def parse_diagram_repos(page: str) -> set[str]:
    """Extract the repository names drawn as nodes in the mermaid diagram.

    A node's id is an arbitrary short handle (``webui``, ``vllm``), so the
    repository name is read from the node's *label*, which is written as
    ``name<br/>description``. Requiring that separator is what distinguishes a
    repository node from decoration such as a ``Users`` actor, with no
    hand-maintained allowlist.

    Args:
        page: Full text of the profile page.

    Returns:
        Repository names, one per node that carries a label separator. Empty
        when the page has no mermaid block.
    """
    block = _MERMAID_RE.search(page)
    if block is None:
        return set()

    names: set[str] = set()
    for raw in block.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("%%") or line in {"end"}:
            continue
        if line.startswith(("flowchart", "graph", "subgraph")):
            # A subgraph title is styled like a node but names a grouping.
            continue
        line = _EDGE_LABEL_RE.sub(" ", line)
        for node in _NODE_RE.finditer(line):
            label = node["label"]
            if not _LABEL_SEP_RE.search(label):
                continue
            names.add(_LABEL_SEP_RE.split(label, maxsplit=1)[0].strip())
    return names


def check_diagram(rows: list[TableRow], nodes: set[str]) -> list[str]:
    """Check that the tables and the mermaid diagram describe the same members.

    Membership is derived from the page itself rather than an exemption list:
    a section marked with :data:`NO_DIAGRAM_MARKER` holds repositories that are
    build-time or orchestration concerns and are deliberately not drawn. A new
    repository added to such a section is therefore exempt automatically.

    Both directions are checked, so a node whose label is misspelled fails as
    an unknown member even though the table row it was meant to match fails as
    a missing node.

    Args:
        rows: Repository rows parsed from the page's tables.
        nodes: Repository names drawn as diagram nodes.

    Returns:
        One human-readable problem per inconsistency; empty when they agree.
    """
    problems: list[str] = []
    expected = {r.slug for r in rows if not r.no_diagram}
    exempt = {r.slug for r in rows if r.no_diagram}

    for slug in sorted(expected - nodes):
        problems.append(f"{slug}: in a profile table but not drawn in the diagram")
    for slug in sorted(nodes & exempt):
        problems.append(f"{slug}: drawn in the diagram but sits under {NO_DIAGRAM_MARKER}")
    for name in sorted(nodes - expected - exempt):
        problems.append(f"{name}: drawn in the diagram but has no profile table row")
    return problems


@dataclass(frozen=True)
class Claim:
    """One anchored claim: a phrase on the page plus the evidence for it."""

    id: str
    quote: str
    kind: str
    value: Any
    repo: str | None = None
    members: tuple[str, ...] = ()
    file: str | None = None
    says: str = ""
    raw: bool = False
    allow_prose: bool = False
    hits: dict[str, int] = field(default_factory=dict)


def load_claims(text: str) -> list[Claim]:
    """Parse and structurally validate the claims sidecar.

    Structural rules are enforced here rather than at evaluation time so that a
    malformed claim can never masquerade as a passing one. In particular a
    claim with no assertion, or with a mistyped assertion key, is an error
    rather than a vacuous success.

    Args:
        text: Contents of ``profile/claims.toml``.

    Returns:
        The declared claims, in file order.

    Raises:
        ClaimsError: The file is not valid TOML, or a claim is malformed.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ClaimsError(f"claims file is not valid TOML: {exc}") from exc

    claims: list[Claim] = []
    seen: set[str] = set()
    for index, entry in enumerate(data.get("claim", [])):
        where = entry.get("id") or f"claim #{index + 1}"

        unknown = sorted(set(entry) - _CLAIM_KEYS)
        if unknown:
            raise ClaimsError(f"{where}: unknown key(s): {', '.join(unknown)}")

        claim_id = entry.get("id")
        if not claim_id:
            raise ClaimsError(f"{where}: missing id")
        if claim_id in seen:
            raise ClaimsError(f"{claim_id}: duplicate claim id")
        seen.add(claim_id)

        quote = entry.get("quote")
        if not quote:
            raise ClaimsError(f"{claim_id}: missing quote (the page phrase it backs)")

        present = [k for k in ASSERTION_KINDS if k in entry]
        if len(present) != 1:
            raise ClaimsError(
                f"{claim_id}: needs exactly one assertion, found {len(present)} ({', '.join(present) or 'none'})"
            )
        kind = present[0]

        repo = entry.get("repo")
        members = tuple(entry.get("in", [repo] if repo else []))

        claims.append(
            Claim(
                id=claim_id,
                quote=quote,
                kind=kind,
                value=entry[kind],
                repo=repo,
                members=members,
                file=entry.get("file"),
                says=entry.get("says", ""),
                raw=bool(entry.get("raw", False)),
                allow_prose=bool(entry.get("allow_prose", False)),
                hits=dict(entry.get("hits", {})),
            )
        )
    return claims


PROSE_SUFFIXES = (".md", ".markdown", ".rst", ".txt")
COMMENT_PREFIXES = ("#", "//")
DEFAULT_HITS = {"min": 1, "max": 2}
MAX_LINE = 10_000


def _resolve(root: Path, rel: str) -> Path | None:
    """Resolve a claim-declared relative path, case-sensitively and safely.

    ``Path.exists`` is case-insensitive on macOS, so a claim naming the wrong
    case would pass locally and then fail on a Linux runner. Each component is
    therefore checked against an exact directory listing. Symlinks and parent
    traversal are refused so a claim cannot reach outside the member checkout.

    Args:
        root: The member checkout.
        rel: Repository-relative path from the claim.

    Returns:
        The resolved path, or ``None`` when it does not exist exactly as named.
    """
    if rel.startswith("/") or ".." in Path(rel).parts:
        return None
    current = root
    for part in Path(rel).parts:
        try:
            if part not in os.listdir(current):
                return None
        except (NotADirectoryError, FileNotFoundError, PermissionError):
            return None
        current = current / part
        if current.is_symlink():
            return None
    return current


def _lines(path: Path, *, raw: bool) -> list[tuple[int, str]]:
    """Read a file as numbered lines, dropping comments unless ``raw``.

    A comment inside a config file is prose, and prose is exactly what must not
    satisfy an anchor: a commented-out or aspirational mention is not evidence
    that the system behaves that way.

    Args:
        path: File to read.
        raw: When true, keep comment lines.

    Returns:
        ``(line number, text)`` pairs, 1-indexed, excluding over-long lines.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > MAX_LINE:
            continue
        if not raw and line.lstrip().startswith(COMMENT_PREFIXES):
            continue
        out.append((number, line))
    return out


def _hit_lines(path: Path, pattern: str, *, raw: bool) -> list[tuple[int, str]]:
    """Return the lines of ``path`` matching ``pattern``."""
    regex = re.compile(pattern)
    return [(n, t) for n, t in _lines(path, raw=raw) if regex.search(t)]


def _check_content(claim: Claim, member: str, root: Path) -> list[str]:
    """Evaluate a file-scoped assertion for one member checkout."""
    if claim.file is None:
        return [f"{member}: {claim.id}: assertion needs a file"]
    if claim.file.endswith(PROSE_SUFFIXES) and not claim.allow_prose:
        return [f"{member}: {claim.id}: {claim.file} is prose; anchor to config or source, or set allow_prose = true"]
    path = _resolve(root, claim.file)
    if path is None or not path.is_file():
        return [f"{member}: {claim.id}: {claim.file} not found"]

    if claim.kind == "json_len":
        try:
            size = len(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            return [f"{member}: {claim.id}: {claim.file} is not countable JSON: {exc}"]
        low, high = claim.value.get("min", 0), claim.value.get("max", 10**9)
        if not low <= size <= high:
            return [f"{member}: {claim.id}: {claim.file} holds {size} entries, expected {low}-{high}"]
        return []

    hits = _hit_lines(path, claim.value, raw=claim.raw)
    if claim.kind == "absent":
        if hits:
            n, text = hits[0]
            return [f"{member}: {claim.id}: {claim.file}:{n} matches forbidden pattern: {text.strip()}"]
        return []

    bounds = {**DEFAULT_HITS, **claim.hits}
    low, high = bounds["min"], bounds["max"]
    if len(hits) < low:
        return [
            f"{member}: {claim.id}: {claim.file} has {len(hits)} hit(s) for {claim.value!r}, expected at least {low}"
        ]
    if len(hits) > high:
        return [
            f"{member}: {claim.id}: {claim.file} has {len(hits)} hit(s) for "
            f"{claim.value!r}; too generic, expected at most {high}"
        ]
    return []


def _check_glob(claim: Claim, member: str, root: Path) -> list[str]:
    """Evaluate a ``glob_count`` assertion for one member checkout."""
    spec = claim.value
    pattern = spec.get("pattern", "")
    if "**" in pattern:
        return [f"{member}: {claim.id}: recursive globs are not allowed"]
    candidates = sorted(q for q in root.glob(pattern) if q.is_file() and not q.is_symlink())
    if not candidates:
        return [f"{member}: {claim.id}: glob {pattern!r} matched no files"]

    containing = spec.get("containing")
    if containing is None:
        found = candidates
    else:
        found = [q for q in candidates if _hit_lines(q, containing, raw=claim.raw)]

    count = len(found)
    if "eq" in spec and count != spec["eq"]:
        return [f"{member}: {claim.id}: {pattern} matched {count} file(s), expected exactly {spec['eq']}"]
    if count < spec.get("min", 0):
        return [f"{member}: {claim.id}: {pattern} matched {count}, expected >= {spec['min']}"]
    if count > spec.get("max", 10**9):
        return [f"{member}: {claim.id}: {pattern} matched {count}, expected <= {spec['max']}"]
    return []


def _check_exists(claim: Claim, member: str, root: Path) -> list[str]:
    """Evaluate an ``exists`` assertion for one member checkout."""
    missing = [rel for rel in claim.value if _resolve(root, rel) is None]
    if missing:
        return [f"{member}: {claim.id}: missing {', '.join(missing)}"]
    return []


def evaluate_claim(claim: Claim, member_paths: dict[str, Path]) -> list[str]:
    """Evaluate one claim against every member checkout it names.

    Args:
        claim: The claim to evaluate.
        member_paths: Member name to checkout path, for every member involved.

    Returns:
        One message per failure, prefixed with the member it came from; empty
        when the claim holds everywhere.
    """
    problems: list[str] = []
    for member in claim.members:
        root = member_paths.get(member)
        if root is None:
            problems.append(f"{member}: {claim.id}: no checkout supplied")
            continue
        if claim.kind == "glob_count":
            problems.extend(_check_glob(claim, member, root))
        elif claim.kind == "exists":
            problems.extend(_check_exists(claim, member, root))
        else:
            problems.extend(_check_content(claim, member, root))
    return problems


@dataclass
class Report:
    """Outcome of one audit run, split by what the caller should do about it."""

    drift: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """Return 2 if the run was unsound, 1 on drift, else 0."""
        if self.errors:
            return 2
        return 1 if self.drift else 0


def audit(
    page: str,
    claims: list[Claim],
    member_paths: dict[str, Path],
    *,
    only_present: bool = False,
) -> Report:
    """Check the page against its claims and the member checkouts.

    Three things are verified: that the page's tables and diagram agree, that
    every repository row is backed by at least one claim whose quoted phrase is
    still in that row, and that each claim's evidence still holds.

    The quote requirement is what keeps the sidecar honest. Without it a claim
    silently outlives the sentence it was written for, and the page can be
    rewritten into falsehood while every anchor still passes.

    Args:
        page: Full text of the profile page.
        claims: Claims parsed from the sidecar.
        member_paths: Member name to checkout path.
        only_present: Skip claims whose checkouts are absent, instead of
            treating the absence as an unsound run.

    Returns:
        A report whose ``exit_code`` distinguishes drift from an unsound run.
    """
    report = Report()
    rows = parse_table_rows(page)
    by_slug = {r.slug: r for r in rows}

    report.drift.extend(check_diagram(rows, parse_diagram_repos(page)))

    for claim in claims:
        if claim.repo is not None and claim.repo not in by_slug:
            report.errors.append(f"{claim.id}: names repo {claim.repo!r}, which has no profile table row")
            continue
        unknown = [m for m in claim.members if m not in by_slug]
        if unknown:
            report.errors.append(f"{claim.id}: names non-member checkout(s): {', '.join(unknown)}")
            continue

        haystack = by_slug[claim.repo].cell if claim.repo is not None else page
        if claim.quote not in haystack:
            where = f"the {claim.repo} row" if claim.repo else "the page"
            report.drift.append(f"{claim.id}: quote {claim.quote!r} is no longer in {where}")
            continue

        missing = [m for m in claim.members if m not in member_paths]
        if missing:
            message = f"{claim.id}: no checkout for {', '.join(missing)}"
            if only_present:
                report.skipped.append(message)
                continue
            report.errors.append(message)
            continue

        report.drift.extend(evaluate_claim(claim, member_paths))

    claimed = {c.repo for c in claims if c.repo is not None}
    for slug in sorted(set(by_slug) - claimed):
        report.drift.append(f"{slug}: has a profile row but no claim backing it")
    return report


def _member_paths(rows: list[TableRow], members_root: Path, overrides: dict[str, Path]) -> dict[str, Path]:
    """Map each member name to its checkout, preferring explicit overrides."""
    paths: dict[str, Path] = {}
    for row in rows:
        if row.slug in overrides:
            paths[row.slug] = overrides[row.slug]
        elif (members_root / row.slug).is_dir():
            paths[row.slug] = members_root / row.slug
    return paths


def main(argv: list[str] | None = None) -> int:
    """Run the profile claims audit.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        0 when every anchored claim holds, 1 on drift, 2 when the run was
        unsound and its result should not be read as either.
    """
    parser = argparse.ArgumentParser(description="Check the profile page's claims against member checkouts.")
    parser.add_argument("--profile", type=Path, default=Path("profile/README.md"))
    parser.add_argument("--claims", type=Path, default=Path("profile/claims.toml"))
    parser.add_argument("--members-root", type=Path, default=Path(".."))
    parser.add_argument(
        "--member",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Checkout for one member, overriding --members-root.",
    )
    parser.add_argument(
        "--only-present",
        action="store_true",
        help="Skip claims whose checkouts are absent instead of failing.",
    )
    parser.add_argument(
        "--list-repos",
        action="store_true",
        help="Print the member names in the page's tables and exit.",
    )
    args = parser.parse_args(argv)

    try:
        page = args.profile.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read profile page: {exc}", file=sys.stderr)
        return 2

    try:
        rows = parse_table_rows(page)
    except PageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list_repos:
        for row in rows:
            print(row.slug)
        return 0

    overrides: dict[str, Path] = {}
    for item in args.member:
        name, _, raw = item.partition("=")
        if not name or not raw:
            print(f"error: --member expects NAME=PATH, got {item!r}", file=sys.stderr)
            return 2
        overrides[name] = Path(raw)

    try:
        claims = load_claims(args.claims.read_text(encoding="utf-8"))
    except (OSError, ClaimsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = audit(
        page,
        claims,
        _member_paths(rows, args.members_root, overrides),
        only_present=args.only_present,
    )

    for line in report.skipped:
        print(f"skipped: {line}")
    for line in report.errors:
        print(f"error: {line}", file=sys.stderr)
    for line in report.drift:
        print(f"drift: {line}", file=sys.stderr)

    if report.exit_code == 0:
        checked = len(claims) - len(report.skipped)
        print(f"Profile claims check OK ({checked} claim(s) verified).")
    return report.exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # an unsound run must never read as drift
        print(f"error: unexpected failure: {exc!r}", file=sys.stderr)
        sys.exit(2)
