#!/usr/bin/env python3
"""Verify the signed provenance of every digest-pinned upstream image in a consumer.

Run from a consumer repo's root (or pass --consumer-root). Scans the repo's
Dockerfiles (``Dockerfile``, ``Dockerfile.*``, ``*.Dockerfile``) and compose
files (``compose*.y[a]ml``, ``docker-compose*.y[a]ml``) for image references
and checks each one against the hub's signer policy
(``configs/image-provenance/signers.toml``, read from next to this script, so
a consumer is always checked against the policy that shipped with the hub
revision it pinned).

A digest pin makes an image immutable, but it does not say who built it:
whoever pushed the tag chose the digest. For images whose publisher signs
what it ships, the signature closes that gap. Every pinned digest of a
listed image must verify against the publisher identity recorded in the
policy, or the check fails. That is the moment it matters most: a Dependabot
PR that moves a pin to a new digest is verified before anyone merges it.

Verification methods (one per policy entry):

* ``github-attestation`` - ``gh attestation verify`` against GitHub artifact
  attestations, pinned to the publisher's repo (and optionally its signing
  workflow, source ref, and GitHub-hosted runners).
* ``cosign-key`` - ``cosign verify --key`` with a public key pinned in the hub.
* ``cosign-keyless`` - ``cosign verify`` against a Sigstore certificate
  identity and OIDC issuer.

Images that are not on the list are reported as unsigned (most upstream
images publish no signature at all, so there is nothing to verify for them).
A listed image pinned by tag only is a failure: without a digest there is
nothing for a signature to cover. Refs that still contain an unresolved
variable after ARG / ``${VAR:-default}`` expansion are counted and skipped.

Parsing is hand-rolled line matching rather than YAML or a Dockerfile
parser, like the other validators here: the script must run in any
consumer's environment with no installed dependencies. Verification shells
out to ``gh`` / ``cosign`` and needs network access; ``gh`` also needs a
token (``GH_TOKEN``).

Writes a markdown table to ``$GITHUB_STEP_SUMMARY`` when that variable is
set, and ``::error`` annotations when running on GitHub Actions.

Exit 0 when every listed image verifies (or there is nothing to check),
1 on a policy failure (an unsigned or wrongly-signed digest, a listed image
without a digest, or any failure the script cannot attribute to the
network), 2 when the check could not run (a verifier missing, a network or
service error, an unreadable policy). Stdlib-only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_POLICY = Path(__file__).resolve().parent.parent / "configs" / "image-provenance" / "signers.toml"

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
DOCKERFILE_RE = re.compile(r"^(?:Dockerfile(?:\..+)?|.+\.Dockerfile)$")
COMPOSE_RE = re.compile(r"^(?:docker-)?compose(?:[.-][\w.-]+)?\.ya?ml$")
IMAGE_LINE_RE = re.compile(r"^\s*(?:-\s+)?image:\s*(.*)$")
SKIP_DIRS = {"node_modules"}

METHODS = ("github-attestation", "cosign-key", "cosign-keyless")
POLICY_FIELDS = {
    "name",
    "method",
    "repo",
    "signer-repo",
    "signer-workflow",
    "source-ref",
    "github-hosted-only",
    "key",
    "ignore-tlog",
    "certificate-identity-regexp",
    "certificate-oidc-issuer",
}

# Substrings (lower-cased) that mark a failure as "could not run" rather than
# "not signed by the expected publisher". Anything else fails closed.
TRANSIENT_MARKERS = (
    "http 5",
    "http 401",
    "rate limit",
    "toomanyrequests",
    "no such host",
    "timeout",
    "timed out",
    "deadline exceeded",
    "connection reset",
    "connection refused",
    "tls handshake",
    "temporary failure",
    "unexpected eof",
    "gh auth login",
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class PolicyError(Exception):
    """The signer policy file is unreadable or malformed."""


@dataclass(frozen=True)
class ImageRef:
    """A parsed image reference.

    Attributes:
        registry: Registry host, ``docker.io`` for Docker Hub.
        repository: Repository path, with ``library/`` added for Docker Hub
            official images.
        tag: The tag, or ``None``.
        digest: The ``sha256:<64 hex>`` digest, or ``None`` when absent or
            malformed.
    """

    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @property
    def name(self) -> str:
        """Return the fully qualified image name without tag or digest."""
        return f"{self.registry}/{self.repository}"

    @property
    def pinned(self) -> str:
        """Return the immutable ``name@digest`` form of the reference."""
        return f"{self.name}@{self.digest}"


@dataclass(frozen=True)
class Signer:
    """One policy entry: an image and how its publisher's signature is checked."""

    name: str
    method: str
    repo: str | None = None
    signer_repo: str | None = None
    signer_workflow: str | None = None
    source_ref: str | None = None
    github_hosted_only: bool = False
    key: Path | None = None
    ignore_tlog: bool = False
    identity_regexp: str | None = None
    oidc_issuer: str | None = None


@dataclass(frozen=True)
class Outcome:
    """The result of verifying one pinned digest.

    Attributes:
        status: ``verified``, ``failed`` (policy failure) or ``error``
            (could not run).
        detail: The signer identity when verified, else the reason.
        signed_at: The signing date (``YYYY-MM-DD``) when the verifier
            reported one.
    """

    status: str
    detail: str
    signed_at: str | None = None


@dataclass(frozen=True)
class Located:
    """An image reference as written at one place in the consumer."""

    path: Path
    lineno: int
    raw: str


@dataclass(frozen=True)
class Failure:
    """A policy failure attributed to one line of the consumer."""

    path: Path
    lineno: int
    raw: str
    reason: str


@dataclass
class Report:
    """Everything one run of the check found."""

    files: list[Path]
    results: dict[str, tuple[ImageRef, Outcome]] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unsigned: set[str] = field(default_factory=set)
    unresolved: list[Located] = field(default_factory=list)

    @property
    def verified(self) -> dict[str, Outcome]:
        """Return the outcomes of the digests that verified, keyed by ``name@digest``."""
        return {pinned: outcome for pinned, (_, outcome) in self.results.items() if outcome.status == "verified"}

    @property
    def exit_code(self) -> int:
        """Return 1 on any policy failure, else 2 on any could-not-run error, else 0."""
        if self.failures:
            return 1
        if self.errors:
            return 2
        return 0


def parse_image_ref(value: str) -> ImageRef | None:
    """Parse an image reference, normalizing Docker Hub names.

    Args:
        value: A reference such as ``python:3.12``,
            ``ghcr.io/org/tool:1.0@sha256:...`` or ``busybox@sha256:...``.

    Returns:
        The parsed reference, or ``None`` when the value is empty, contains
        whitespace, or still holds an unresolved ``$`` variable.
    """
    value = value.strip()
    if not value or "$" in value or any(c.isspace() for c in value):
        return None
    name_tag, sep, digest = value.partition("@")
    pinned_digest = digest if sep and DIGEST_RE.match(digest) else None

    slash, colon = name_tag.rfind("/"), name_tag.rfind(":")
    name, tag = (name_tag[:colon], name_tag[colon + 1 :]) if colon > slash else (name_tag, None)

    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, repository = "docker.io", name
    if registry in {"index.docker.io", "registry-1.docker.io"}:
        registry = "docker.io"
    if registry == "docker.io" and "/" not in repository:
        repository = f"library/{repository}"
    if not repository or tag == "":
        return None
    return ImageRef(registry, repository, tag, pinned_digest)


def _expand(value: str, env: dict[str, str]) -> str:
    """Expand ``$NAME``, ``${NAME}`` and ``${NAME:-default}`` from ``env``.

    Args:
        value: The text to expand.
        env: Known variable values.

    Returns:
        The expanded text; variables with neither a value nor a default are
        left as written, so callers can tell the ref is unresolved.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(3)
        default = match.group(2)
        if env.get(name):
            return env[name]
        if default is not None:
            return default
        return match.group(0)

    return VAR_RE.sub(replace, value)


def _logical_lines(text: str) -> Iterator[tuple[int, str]]:
    """Yield Dockerfile instructions with backslash continuations joined.

    Args:
        text: Dockerfile contents.

    Yields:
        ``(first line number, instruction)`` pairs, skipping blank lines and
        comments (including comment lines inside a continuation).
    """
    parts: list[str] = []
    start = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not parts:
            start = lineno
        if stripped.endswith("\\"):
            parts.append(stripped[:-1].strip())
            continue
        parts.append(stripped)
        yield start, " ".join(parts)
        parts = []
    if parts:
        yield start, " ".join(parts)


def _unquote(value: str) -> str:
    """Strip one pair of surrounding quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def dockerfile_refs(text: str) -> list[tuple[int, str]]:
    """Find the image references in a Dockerfile.

    Collects ``FROM`` images and ``COPY``/``ADD --from=`` images, expanding
    ``ARG`` defaults, and skips build stages (by name or index) and
    ``scratch``.

    Args:
        text: Dockerfile contents.

    Returns:
        ``(line number, expanded reference)`` pairs in file order.
    """
    env: dict[str, str] = {}
    stages: set[str] = set()
    refs: list[tuple[int, str]] = []
    for lineno, line in _logical_lines(text):
        tokens = line.split()
        keyword = tokens[0].upper()
        if keyword == "ARG":
            for token in tokens[1:]:
                name, sep, default = token.partition("=")
                if sep:
                    env[name] = _expand(_unquote(default), env)
        elif keyword == "FROM":
            args = [t for t in tokens[1:] if not t.startswith("--")]
            if not args:
                continue
            if len(args) >= 3 and args[1].lower() == "as":
                stages.add(args[2].lower())
            image = _expand(args[0], env)
            if image.lower() not in stages and image.lower() != "scratch":
                refs.append((lineno, image))
        elif keyword in {"COPY", "ADD"}:
            for token in tokens[1:]:
                if token.startswith("--from="):
                    source = _expand(token.removeprefix("--from="), env)
                    if source.lower() not in stages and not source.isdigit():
                        refs.append((lineno, source))
    return refs


def compose_refs(text: str) -> list[tuple[int, str]]:
    """Find the ``image:`` references in a compose file.

    Handles quotes, trailing comments, YAML anchors (``&name``) and
    ``${VAR:-default}`` defaults; YAML aliases (``*name``) are skipped
    because the anchored value is reported where it is defined.

    Args:
        text: Compose file contents.

    Returns:
        ``(line number, reference)`` pairs in file order.
    """
    refs: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            continue
        match = IMAGE_LINE_RE.match(raw)
        if match is None:
            continue
        value = match.group(1).strip()
        if value.startswith("&"):
            value = value.partition(" ")[2].strip()
        if value[:1] in {'"', "'"}:
            end = value.find(value[0], 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if not value or value.startswith("*"):
            continue
        refs.append((lineno, _expand(value, {})))
    return refs


def target_files(consumer_root: Path) -> list[Path]:
    """Collect the Dockerfiles and compose files to check.

    Args:
        consumer_root: The consumer repo root to inspect.

    Returns:
        Matching files sorted by their path relative to the root. Hidden
        directories (``.git``, a ``.nos-tromo-github-ref`` checkout of this
        repo, ...) and ``node_modules`` are skipped.
    """
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(consumer_root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
        for filename in filenames:
            is_dockerfile = DOCKERFILE_RE.match(filename) and not filename.endswith(".dockerignore")
            if is_dockerfile or COMPOSE_RE.match(filename):
                found.append(Path(directory) / filename)
    return sorted(found, key=lambda p: p.relative_to(consumer_root).as_posix())


def refs_in_file(path: Path) -> list[tuple[int, str]]:
    """Return the image references in one Dockerfile or compose file.

    Args:
        path: A file returned by :func:`target_files`.

    Returns:
        ``(line number, reference)`` pairs.
    """
    text = path.read_text()
    return compose_refs(text) if COMPOSE_RE.match(path.name) else dockerfile_refs(text)


def load_policy(path: Path) -> dict[str, Signer]:
    """Load and validate the signer policy.

    Args:
        path: The ``signers.toml`` file. Key paths in it resolve relative to
            the file's directory.

    Returns:
        Policy entries keyed by fully qualified image name.

    Raises:
        PolicyError: The file is unreadable or an entry is malformed.
    """
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"cannot read signer policy {path}: {exc}") from exc

    entries = data.get("image", [])
    if not isinstance(entries, list):
        raise PolicyError(f"{path}: expected [[image]] tables")

    policy: dict[str, Signer] = {}
    for index, entry in enumerate(entries, start=1):
        name = entry.get("name")
        if not name:
            raise PolicyError(f"{path}: entry {index} has no name")
        method = entry.get("method")
        if method not in METHODS:
            raise PolicyError(f"{name}: unknown method {method!r} (expected one of {', '.join(METHODS)})")
        if name in policy:
            raise PolicyError(f"{name}: duplicate entry")
        unknown = sorted(set(entry) - POLICY_FIELDS)
        if unknown:
            raise PolicyError(f"{name}: unknown field(s) {', '.join(unknown)}")

        key: Path | None = None
        if method == "github-attestation" and not entry.get("repo"):
            raise PolicyError(f"{name}: github-attestation needs a repo")
        if method == "cosign-key":
            if not entry.get("key"):
                raise PolicyError(f"{name}: cosign-key needs a key file")
            key = path.parent / entry["key"]
            if not key.is_file():
                raise PolicyError(f"{name}: key file {key} not found")
        if method == "cosign-keyless" and not (
            entry.get("certificate-identity-regexp") and entry.get("certificate-oidc-issuer")
        ):
            raise PolicyError(f"{name}: cosign-keyless needs certificate-identity-regexp and certificate-oidc-issuer")

        policy[name] = Signer(
            name=name,
            method=method,
            repo=entry.get("repo"),
            signer_repo=entry.get("signer-repo"),
            signer_workflow=entry.get("signer-workflow"),
            source_ref=entry.get("source-ref"),
            github_hosted_only=bool(entry.get("github-hosted-only", False)),
            key=key,
            ignore_tlog=bool(entry.get("ignore-tlog", False)),
            identity_regexp=entry.get("certificate-identity-regexp"),
            oidc_issuer=entry.get("certificate-oidc-issuer"),
        )
    return policy


def verify_command(signer: Signer, ref: ImageRef) -> list[str]:
    """Build the verifier command line for one pinned digest.

    Args:
        signer: The policy entry for the image.
        ref: The pinned reference (must carry a digest).

    Returns:
        The argv to run.
    """
    if signer.method == "github-attestation":
        cmd = ["gh", "attestation", "verify", f"oci://{ref.pinned}", "--repo", str(signer.repo)]
        if signer.signer_repo:
            cmd += ["--signer-repo", signer.signer_repo]
        if signer.signer_workflow:
            cmd += ["--signer-workflow", signer.signer_workflow]
        if signer.source_ref:
            cmd += ["--source-ref", signer.source_ref]
        if signer.github_hosted_only:
            cmd.append("--deny-self-hosted-runners")
        return [*cmd, "--format", "json"]
    if signer.method == "cosign-key":
        cmd = ["cosign", "verify", "--key", str(signer.key)]
        if signer.ignore_tlog:
            cmd.append("--insecure-ignore-tlog=true")
        return [*cmd, ref.pinned]
    return [
        "cosign",
        "verify",
        "--certificate-identity-regexp",
        str(signer.identity_regexp),
        "--certificate-oidc-issuer",
        str(signer.oidc_issuer),
        ref.pinned,
    ]


def classify_failure(output: str) -> int:
    """Classify a verifier failure.

    Args:
        output: The verifier's combined stderr and stdout.

    Returns:
        ``2`` when the output points at the network, a service, or missing
        credentials (the check could not run); ``1`` otherwise, including
        for output nobody anticipated, so an unknown failure fails closed.
    """
    lowered = output.lower()
    return 2 if any(marker in lowered for marker in TRANSIENT_MARKERS) else 1


def _first_json(stdout: str) -> object:
    """Parse the verifier's JSON output, tolerating trailing lines."""
    for candidate in (stdout, *stdout.splitlines()):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _describe(signer: Signer, stdout: str) -> tuple[str, str | None]:
    """Extract the signer identity and signing date from verifier output.

    Args:
        signer: The policy entry that was verified.
        stdout: The verifier's JSON output.

    Returns:
        ``(signer identity, signing date or None)``. Falls back to the
        policy's own description when the output lacks the fields.
    """
    data = _first_json(stdout)
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    if signer.method == "github-attestation":
        result = first.get("verificationResult") or {}
        certificate = (result.get("signature") or {}).get("certificate") or {}
        uri = certificate.get("buildSignerURI") or ""
        stamps = result.get("verifiedTimestamps") or [{}]
        stamp = stamps[0].get("timestamp") if isinstance(stamps[0], dict) else None
        identity = uri.removeprefix("https://github.com/") or f"repo {signer.repo}"
        return identity, (stamp[:10] if stamp else None)

    optional = first.get("optional") or {}
    subject = optional.get("Subject") or ""
    integrated = ((optional.get("Bundle") or {}).get("Payload") or {}).get("integratedTime")
    if subject:
        identity = subject.removeprefix("https://github.com/")
    elif signer.key is not None:
        identity = f"key {signer.key.name}"
    else:
        identity = signer.name
    signed_at = datetime.fromtimestamp(integrated, tz=UTC).date().isoformat() if integrated else None
    return identity, signed_at


def verify(signer: Signer, ref: ImageRef, runner: Runner = subprocess.run) -> Outcome:
    """Verify one pinned digest against its policy entry.

    Args:
        signer: The policy entry for the image.
        ref: The pinned reference.
        runner: ``subprocess.run`` or a test double with the same signature.

    Returns:
        The outcome; never raises for verifier failures.
    """
    cmd = verify_command(signer, ref)
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=180, check=False)
    except FileNotFoundError:
        return Outcome("error", f"{cmd[0]} is not installed (needed to verify {signer.name})")
    except subprocess.TimeoutExpired:
        return Outcome("error", f"{cmd[0]} timed out")
    if proc.returncode == 0:
        identity, signed_at = _describe(signer, proc.stdout or "")
        return Outcome("verified", identity, signed_at)
    output = f"{proc.stderr or ''}\n{proc.stdout or ''}"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    reason = lines[0] if lines else f"{cmd[0]} exited {proc.returncode}"
    return Outcome("error" if classify_failure(output) == 2 else "failed", reason)


def check(
    consumer_root: Path,
    policy: dict[str, Signer],
    runner: Runner = subprocess.run,
    files: list[Path] | None = None,
) -> Report:
    """Check every image reference in a consumer against the policy.

    Each distinct pinned digest of a listed image is verified once, however
    many files pin it.

    Args:
        consumer_root: The consumer repo root.
        policy: The loaded signer policy.
        runner: ``subprocess.run`` or a test double with the same signature.
        files: Files to scan; defaults to :func:`target_files`.

    Returns:
        The full report.
    """
    report = Report(files=target_files(consumer_root) if files is None else files)
    pending: dict[str, tuple[Signer, ImageRef, list[Located]]] = {}
    for path in report.files:
        for lineno, raw in refs_in_file(path):
            ref = parse_image_ref(raw)
            if ref is None:
                if "$" in raw:
                    report.unresolved.append(Located(path, lineno, raw))
                continue
            signer = policy.get(ref.name)
            if signer is None:
                if ref.digest:
                    report.unsigned.add(ref.name)
                continue
            if ref.digest is None:
                reason = (
                    f"{ref.name} is on the signer list but not digest-pinned; pin it as "
                    f"{raw}@sha256:<digest> so its signature can be verified"
                )
                report.failures.append(Failure(path, lineno, raw, reason))
                continue
            pending.setdefault(ref.pinned, (signer, ref, []))[2].append(Located(path, lineno, raw))

    for pinned, (signer, ref, places) in pending.items():
        outcome = verify(signer, ref, runner)
        report.results[pinned] = (ref, outcome)
        if outcome.status == "failed":
            report.failures.extend(Failure(p.path, p.lineno, p.raw, outcome.detail) for p in places)
        elif outcome.status == "error":
            report.errors.append(f"{pinned}: could not verify: {outcome.detail}")
    return report


def _cell(text: str) -> str:
    """Escape text for a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def render_summary(report: Report) -> str:
    """Render the report as markdown for the GitHub job summary.

    Args:
        report: The finished report.

    Returns:
        A markdown section: a table of the listed images that were checked,
        then the unsigned upstream images and any skipped refs.
    """
    lines = ["### Image provenance", ""]
    rows: list[str] = []
    for ref, outcome in report.results.values():
        short = (ref.digest or "")[:19]
        if outcome.status == "verified":
            result, signer, signed = "verified", outcome.detail, outcome.signed_at or ""
        else:
            label = "FAILED" if outcome.status == "failed" else "could not verify"
            result, signer, signed = f"**{label}**: {outcome.detail}", "", ""
        rows.append(f"| `{ref.name}` | `{short}` | {_cell(result)} | {_cell(signer)} | {signed} |")
    for failure in report.failures:
        ref = parse_image_ref(failure.raw)
        if ref is not None and ref.digest is None:
            rows.append(f"| `{ref.name}` | — | **FAILED**: not digest-pinned | | |")
    if rows:
        lines += ["| Image | Digest | Result | Signed by | Signed |", "|---|---|---|---|---|", *rows, ""]
    else:
        lines += ["No pinned image here is published with a signature to verify.", ""]
    if report.unsigned:
        names = ", ".join(f"`{name}`" for name in sorted(report.unsigned))
        lines += [f"Unsigned upstream images (nothing to verify): {names}", ""]
    if report.unresolved:
        lines += [f"{len(report.unresolved)} ref(s) with unresolved variables were skipped.", ""]
    return "\n".join(lines)


def _relative(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root`` when possible."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _print_report(report: Report, consumer_root: Path) -> None:
    """Print the human-readable result and GitHub annotations."""
    on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if report.unsigned:
        print(f"unsigned upstream images (nothing to verify): {', '.join(sorted(report.unsigned))}")
    if report.exit_code == 0:
        print(
            f"image provenance check OK ({len(report.verified)} verified, "
            f"{len(report.unsigned)} unsigned upstream image(s), {len(report.files)} file(s) scanned)."
        )
        return
    if report.failures:
        print("image provenance check FAILED - signed images did not verify.\n", file=sys.stderr)
        for failure in report.failures:
            rel = _relative(failure.path, consumer_root)
            print(f"{rel}:{failure.lineno}: {failure.raw}: {failure.reason}", file=sys.stderr)
            if on_actions:
                print(f"::error file={rel},line={failure.lineno}::{failure.raw}: {failure.reason}", file=sys.stderr)
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
        if on_actions:
            print(f"::error::{error}", file=sys.stderr)


def main(argv: list[str] | None = None, runner: Runner = subprocess.run) -> int:
    """Run the provenance check and return a shell exit code.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).
        runner: ``subprocess.run`` or a test double with the same signature.

    Returns:
        ``0`` when every listed image verifies or there is nothing to check,
        ``1`` on a policy failure, ``2`` when the check could not run.
    """
    p = argparse.ArgumentParser(description="Verify signed provenance of digest-pinned upstream images.")
    p.add_argument("--consumer-root", type=Path, default=Path())
    p.add_argument("--signers", type=Path, default=DEFAULT_POLICY, help="signer policy (default: the hub's)")
    args = p.parse_args(argv)

    files = target_files(args.consumer_root)
    if not files:
        print("no Dockerfiles or compose files; skipping (no image surface).")
        return 0
    try:
        policy = load_policy(args.signers)
    except PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = check(args.consumer_root, policy, runner, files=files)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a") as handle:
            handle.write(render_summary(report) + "\n")
    _print_report(report, args.consumer_root)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
