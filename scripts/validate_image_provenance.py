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
nothing for a signature to cover.

Parsing is hand-rolled line matching rather than YAML or a Dockerfile
parser, like the other validators here: the script must run in any
consumer's environment with no installed dependencies. Because a line
parser can miss syntax it does not model, the check fails closed around
listed images instead of trusting the parse:

* a listed image name on any non-comment line that the parser did not read
  as an image reference (a YAML block scalar or flow mapping, an ARG used
  outside FROM, ...) is a failure;
* a ref whose unresolved variables could stand for a listed image is a
  failure: the text before the first ``$`` is a prefix of a listed name or
  names one, or the registry segment holds a variable and some value of the
  variables would make a listed repository the ref's repository
  (``${REGISTRY}/qdrant/qdrant``). Refs with no literal name left are
  skipped with a warning.

ARG defaults and compose ``${VAR:-default}`` values are expanded; a
Dockerfile ``# escape=`` directive is honored. Verification shells out to
``gh`` / ``cosign`` and needs network access; ``gh`` also needs a token
(``GH_TOKEN``).

Writes a markdown table to ``$GITHUB_STEP_SUMMARY`` when that variable is
set, and ``::error`` / ``::warning`` annotations on GitHub Actions.

Exit 0 when every listed image verifies (or there is nothing to check),
1 on a policy failure (an unsigned or wrongly-signed digest, a listed image
without a digest or outside a recognized reference, or any failure the
script cannot attribute to the network), 2 when the check could not run (a
verifier missing, a network or service error, an unreadable policy,
Dockerfile or compose file). Both non-zero codes fail CI; 2 only labels the
cause, so a verifier message cannot turn a failure into a pass.
Stdlib-only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
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
DIRECTIVE_RE = re.compile(r"^#\s*([A-Za-z]+)\s*=\s*(\S+)\s*$")
DOCKERFILE_RE = re.compile(r"^(?:Dockerfile(?:\..+)?|.+\.Dockerfile)$")
COMPOSE_RE = re.compile(r"^(?:docker-)?compose(?:[.-][\w.-]+)?\.ya?ml$")
IMAGE_LINE_RE = re.compile(r"^\s*(?:-\s+)?image:\s*(.*)$")
DOCKER_HUB_ALIASES = ("index.docker.io", "registry-1.docker.io")
# Tool and dependency directories: never consumer-authored image refs.
SKIP_DIRS = {
    ".git",
    ".nos-tromo-github-ref",
    ".venv",
    "venv",
    ".tox",
    "node_modules",
    ".pnpm-store",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}

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

# Verifier messages (lower-cased) that mean the signature or identity check
# itself ran and failed. They win over the network patterns below, because a
# mismatch message echoes the identity it found, which the publisher of the
# image controls.
POLICY_MARKERS = (
    "http 404",
    "no signatures found",
    "no matching signatures",
    "none of the expected identities",
    "got subjects",
    "verifying with issuer",
    "manifest unknown",
)
# Network, service and credential errors: the check could not run.
TRANSIENT_RE = re.compile(
    r"http 5\d\d|http 401|rate limit exceeded|toomanyrequests|no such host|i/o timeout|tls handshake timeout"
    r"|deadline exceeded|connection reset by peer|connection refused|temporary failure in name resolution"
    r"|unexpected eof|gh auth login"
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
class FileRef:
    """An image reference as parsed from one file.

    Attributes:
        lineno: The line the instruction or ``image:`` key starts on.
        raw: The reference after variable expansion.
        cover: Every line the reference was read from: continuation lines,
            and the lines of the ARG defaults it expanded.
    """

    lineno: int
    raw: str
    cover: frozenset[int]


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
    unreadable: dict[Path, str] = field(default_factory=dict)

    @property
    def verified(self) -> dict[str, Outcome]:
        """Return the outcomes of the digests that verified, keyed by ``name@digest``."""
        return {pinned: outcome for pinned, (_, outcome) in self.results.items() if outcome.status == "verified"}

    @property
    def exit_code(self) -> int:
        """Return 1 on any policy failure, else 2 when anything could not be checked, else 0."""
        if self.failures:
            return 1
        if self.errors or self.unreadable:
            return 2
        return 0


def _registry_host(host: str) -> str:
    """Return the policy spelling of a registry host.

    Hosts are case-insensitive and ``:443`` is the default port, so
    ``GHCR.IO`` and ``ghcr.io:443`` both name ``ghcr.io``. Docker Hub's other
    hosts name ``docker.io``.
    """
    host = host.lower().removesuffix(":443")
    return "docker.io" if host in DOCKER_HUB_ALIASES else host


def parse_image_ref(value: str) -> ImageRef | None:
    """Parse an image reference, normalizing the registry host and Docker Hub names.

    Args:
        value: A reference such as ``python:3.12``,
            ``ghcr.io/org/tool:1.0@sha256:...`` or ``busybox@sha256:...``.

    Returns:
        The parsed reference, or ``None`` when the value is empty, contains
        whitespace, or still holds an unresolved ``$`` variable. The registry
        is spelled as :func:`_registry_host` returns it, so every spelling of
        a listed image is looked up under its policy name.
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
        registry, repository = _registry_host(first), rest
    else:
        registry, repository = "docker.io", name
    if registry == "docker.io" and "/" not in repository:
        repository = f"library/{repository}"
    if not repository or tag == "":
        return None
    return ImageRef(registry, repository, tag, pinned_digest)


def _expand(value: str, env: dict[str, str], used: set[str] | None = None) -> str:
    """Expand ``$NAME``, ``${NAME}`` and ``${NAME:-default}`` from ``env``.

    Args:
        value: The text to expand.
        env: Known variable values.
        used: When given, receives the names whose value from ``env`` was
            substituted.

    Returns:
        The expanded text; variables with neither a value nor a default are
        left as written, so callers can tell the ref is unresolved.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(3)
        default = match.group(2)
        if env.get(name):
            if used is not None:
                used.add(name)
            return env[name]
        if default is not None:
            return default
        return match.group(0)

    return VAR_RE.sub(replace, value)


def _dockerfile_lines(text: str) -> list[str]:
    """Split a Dockerfile into lines the way BuildKit does.

    BuildKit breaks lines at newlines only. A carriage return, vertical tab
    or form feed inside a line separates words like a space, so ``FROM``,
    a form feed and an image name is a ``FROM`` line. ``str.splitlines``
    would break that line in two and hide the image from the parser.
    """
    return text.split("\n")


def _logical_lines(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield Dockerfile instructions with line continuations joined.

    The continuation character is a backslash unless a ``# escape=`` parser
    directive at the top of the file sets another one.

    Args:
        text: Dockerfile contents.

    Yields:
        ``(first line, last line, instruction)`` triples, skipping blank
        lines and comments (including comment lines inside a continuation).
    """
    lines = _dockerfile_lines(text)
    escape = "\\"
    for raw in lines:
        directive = DIRECTIVE_RE.match(raw.strip())
        if directive is None:
            break
        if directive.group(1).lower() == "escape" and directive.group(2) in {"\\", "`"}:
            escape = directive.group(2)

    parts: list[str] = []
    start = end = 0
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not parts:
            start = lineno
        end = lineno
        if stripped.endswith(escape):
            parts.append(stripped[: -len(escape)].strip())
            continue
        parts.append(stripped)
        yield start, lineno, " ".join(parts)
        parts = []
    if parts:
        yield start, end, " ".join(parts)


def _unquote(value: str) -> str:
    """Strip one pair of surrounding quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _dockerfile_refs(text: str) -> list[FileRef]:
    """Find the image references in a Dockerfile, with the lines they cover.

    Args:
        text: Dockerfile contents.

    Returns:
        One entry per ``FROM`` image and ``COPY``/``ADD --from=`` image, in
        file order.
    """
    env: dict[str, tuple[str, frozenset[int]]] = {}
    stages: set[str] = set()
    refs: list[FileRef] = []

    def expand(value: str, span: frozenset[int]) -> tuple[str, frozenset[int]]:
        used: set[str] = set()
        expanded = _expand(value, {name: v for name, (v, _) in env.items()}, used)
        lines = set(span)
        for name in used:
            lines |= env[name][1]
        return expanded, frozenset(lines)

    for start, end, line in _logical_lines(text):
        span = frozenset(range(start, end + 1))
        tokens = line.split()
        keyword = tokens[0].upper()
        if keyword == "ARG":
            for token in tokens[1:]:
                name, sep, default = token.partition("=")
                if sep:
                    env[name] = expand(_unquote(default), span)
        elif keyword == "FROM":
            args = [t for t in tokens[1:] if not t.startswith("--")]
            if not args:
                continue
            if len(args) >= 3 and args[1].lower() == "as":
                stages.add(args[2].lower())
            image, cover = expand(args[0], span)
            if image.lower() not in stages and image.lower() != "scratch":
                refs.append(FileRef(start, image, cover))
        elif keyword in {"COPY", "ADD"}:
            for token in tokens[1:]:
                if token.startswith("--from="):
                    source, cover = expand(token.removeprefix("--from="), span)
                    if source.lower() not in stages and not source.isdigit():
                        refs.append(FileRef(start, source, cover))
    return refs


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
    return [(ref.lineno, ref.raw) for ref in _dockerfile_refs(text)]


def _compose_refs(text: str) -> list[FileRef]:
    """Find the ``image:`` references in a compose file (see :func:`compose_refs`)."""
    return [FileRef(lineno, raw, frozenset({lineno})) for lineno, raw in compose_refs(text)]


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
        Matching files sorted by their path relative to the root. Tool and
        dependency directories (:data:`SKIP_DIRS`: ``.git``, a
        ``.nos-tromo-github-ref`` checkout of this repo, virtualenvs,
        ``node_modules``, ...) and nested checkouts (any subdirectory with
        its own ``.git``, e.g. a local worktree) are skipped; other hidden
        directories such as ``.devcontainer`` are scanned.
    """
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(consumer_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (Path(directory) / d / ".git").exists()]
        for filename in filenames:
            is_dockerfile = DOCKERFILE_RE.match(filename) and not filename.endswith(".dockerignore")
            if is_dockerfile or COMPOSE_RE.match(filename):
                found.append(Path(directory) / filename)
    return sorted(found, key=lambda p: p.relative_to(consumer_root).as_posix())


def _is_compose(path: Path) -> bool:
    """Return whether a target file is a compose file (else a Dockerfile)."""
    return bool(COMPOSE_RE.match(path.name))


def _name_forms(name: str) -> list[str]:
    """Return the spellings a fully qualified image name can be written in.

    Args:
        name: A policy image name, e.g. ``docker.io/qdrant/qdrant``.

    Returns:
        The name with and without the default ``:443`` port. For Docker Hub
        images, also the same under Docker Hub's other hosts, and the
        registry-less spellings. Hosts are spelled in lower case here; the
        callers match them in any case.
    """
    registry, _, repository = name.partition("/")
    hosts = [registry]
    bare: list[str] = []
    if registry == "docker.io":
        hosts += DOCKER_HUB_ALIASES
        bare.append(repository)
        if repository.startswith("library/"):
            bare.append(repository.removeprefix("library/"))
    return [f"{host}{port}/{repository}" for host in hosts for port in ("", ":443")] + bare


def _mention_pattern(name: str) -> re.Pattern[str]:
    """Compile a pattern that finds any spelling of a listed image name in a line.

    The registry host matches in any case. The repository path does not: a
    valid reference spells it in lower case, and a case-blind match would
    flag prose such as a label that names ``Example/DB``.

    Args:
        name: A policy image name.

    Returns:
        A pattern matching any of :func:`_name_forms` as a whole name.
    """
    alternatives: list[str] = []
    for form in sorted(_name_forms(name), key=len, reverse=True):
        host, sep, path = form.partition("/")
        if sep and ("." in host or ":" in host):
            alternatives.append(f"(?i:{re.escape(host)})/{re.escape(path)}")
        else:
            alternatives.append(re.escape(form))
    return re.compile(r"(?<![\w./-])(?:" + "|".join(alternatives) + r")(?![\w./-])")


def _mask_variables(raw: str) -> str:
    """Replace every unresolved variable with a NUL wildcard marker.

    Handles ``$NAME``, ``${...}`` with any modifier (``:?``, ``:+``, ...) and
    nested ``${A:+${B}}``, which a plain ``[^}]*`` pattern would cut short.

    Args:
        raw: A ref that still contains ``$`` after expansion.

    Returns:
        The ref with each variable collapsed to one NUL character.
    """
    out: list[str] = []
    i = 0
    while i < len(raw):
        if raw.startswith("${", i):
            depth, j = 1, i + 2
            while j < len(raw) and depth:
                if raw.startswith("${", j):
                    depth, j = depth + 1, j + 2
                    continue
                if raw[j] == "}":
                    depth -= 1
                j += 1
            out.append("\0")
            i = j
            continue
        name = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*", raw[i:])
        if name:
            out.append("\0")
            i += name.end()
            continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def _could_be_repository(masked: str, path: str) -> bool:
    """Return whether some value of the variables makes ``path`` the ref's repository.

    Every NUL marker is a wildcard. The repository starts at the beginning (a
    Docker Hub short name), right after a variable, or right after a
    registry segment without a ``/``; it ends at a tag, a digest, a
    variable, or the end. A candidate must keep at least one literal letter
    or digit, so a ref that is nothing but variables only warns.

    Args:
        masked: The ref after :func:`_mask_variables`.
        path: A listed repository path, e.g. ``qdrant/qdrant``.

    Returns:
        ``True`` when the literal parts are consistent with ``path``.
    """
    n = len(masked)
    for i in range(n):
        head = masked[:i]
        if not (i == 0 or head.endswith("\0") or re.fullmatch(r"[^/]*/", head)):
            continue
        for j in range(i + 1, n + 1):
            if not (j == n or masked[j] in ":@\0" or masked[j - 1] == "\0"):
                continue
            piece = masked[i:j]
            if not any(c.isalnum() for c in piece.replace("\0", "")):
                continue
            pattern = "".join("*" if c == "\0" else glob.escape(c) for c in piece)
            if fnmatch.fnmatchcase(path, pattern):
                return True
    return False


def _hidden_listed_name(raw: str, policy: dict[str, Signer]) -> str | None:
    """Return the listed image an unresolved variable could stand for.

    Args:
        raw: A ref that still contains ``$`` after expansion.
        policy: The loaded signer policy.

    Returns:
        A listed image name when the text before the first ``$`` (its host
        in any case) is a prefix of one of its spellings or names it
        outright (followed by a tag, a digest, or nothing), or, when the
        registry segment itself holds a variable (``${REGISTRY}/org/repo``,
        ``${HOST}.io/...``), when some value of the variables would make a
        listed repository path the ref's repository; ``None`` otherwise.
    """
    head, sep, rest = raw.split("$", 1)[0].partition("/")
    prefix = head.lower() + sep + rest
    masked = _mask_variables(raw)
    variable_registry = "\0" in masked.split("/", 1)[0]
    for name in policy:
        if prefix:
            for form in _name_forms(name):
                if form.startswith(prefix):
                    return name
                if prefix.startswith(form) and prefix[len(form) : len(form) + 1] in {"", ":", "@"}:
                    return name
        if not variable_registry:
            continue
        registry, _, repository = name.partition("/")
        paths = {repository}
        if registry == "docker.io" and repository.startswith("library/"):
            paths.add(repository.removeprefix("library/"))
        if any(_could_be_repository(masked, path) for path in paths):
            return name
    return None


def _unrecognized_mentions(
    path: Path, text: str, refs: list[FileRef], policy: dict[str, Signer], flagged: set[int]
) -> list[Failure]:
    """Find listed image names on lines the parser did not read as references.

    Args:
        path: The file being checked.
        text: Its contents.
        refs: The references parsed from it.
        policy: The loaded signer policy.
        flagged: Lines already reported, so a line fails once.

    Returns:
        One failure per non-comment line that mentions a listed image (in
        any spelling) without being part of a parsed reference to it.
    """
    covered: dict[str, set[int]] = {}
    for ref in refs:
        parsed = parse_image_ref(ref.raw)
        if parsed is not None and parsed.name in policy:
            covered.setdefault(parsed.name, set()).update(ref.cover)
    patterns = {name: _mention_pattern(name) for name in policy}
    compose = _is_compose(path)
    failures: list[Failure] = []
    for lineno, raw in enumerate(text.splitlines() if compose else _dockerfile_lines(text), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or lineno in flagged:
            continue
        if compose:
            line = re.split(r"\s+#", line, maxsplit=1)[0]
        for name, pattern in patterns.items():
            if pattern.search(line) and lineno not in covered.get(name, set()):
                reason = (
                    f"{name} appears here but not as a recognized image reference (FROM, COPY --from= or "
                    "image:); write it as a plain reference so its signature can be verified"
                )
                failures.append(Failure(path, lineno, line, reason))
                flagged.add(lineno)
                break
    return failures


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
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
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
    if any(marker in lowered for marker in POLICY_MARKERS):
        return 1
    return 2 if TRANSIENT_RE.search(lowered) else 1


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
    many files pin it. Files are decoded as UTF-8 with no newline
    translation, so each parser sees the line breaks the real tool sees, and
    a leading byte-order mark is dropped as BuildKit drops it. A file that
    cannot be read is recorded in ``unreadable`` and makes the check exit 2.

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
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            report.unreadable[path] = "not valid UTF-8"
            continue
        except OSError as exc:
            report.unreadable[path] = exc.strerror or type(exc).__name__
            continue
        refs = _compose_refs(text) if _is_compose(path) else _dockerfile_refs(text)
        flagged: set[int] = set()
        for file_ref in refs:
            lineno, raw = file_ref.lineno, file_ref.raw
            ref = parse_image_ref(raw)
            if ref is None:
                if "$" not in raw:
                    continue
                hidden = _hidden_listed_name(raw, policy)
                if hidden is None:
                    report.unresolved.append(Located(path, lineno, raw))
                    continue
                reason = (
                    f"unresolved variable in a ref that may name {hidden}, a signed image; write refs to "
                    "signed images literally so they can be verified"
                )
                report.failures.append(Failure(path, lineno, raw, reason))
                flagged.add(lineno)
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
                flagged.add(lineno)
                continue
            pending.setdefault(ref.pinned, (signer, ref, []))[2].append(Located(path, lineno, raw))
        report.failures.extend(_unrecognized_mentions(path, text, refs, policy, flagged))

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
        then the unsigned upstream images, any files that could not be read,
        and any skipped refs.
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
        if ref is not None and ref.digest is not None and ref.pinned in report.results:
            continue  # already a row above, from the verifier's result
        where = f"{failure.path.name}:{failure.lineno}"
        rows.append(f"| `{_cell(failure.raw)}` | — | **FAILED**: {_cell(failure.reason)} ({where}) | | |")
    if rows:
        lines += ["| Image | Digest | Result | Signed by | Signed |", "|---|---|---|---|---|", *rows, ""]
    elif not report.unreadable:
        lines += ["No pinned image here is published with a signature to verify.", ""]
    if report.unsigned:
        names = ", ".join(f"`{name}`" for name in sorted(report.unsigned))
        lines += [f"Unsigned upstream images (nothing to verify): {names}", ""]
    if report.unreadable:
        files = ", ".join(f"`{path.name}` ({reason})" for path, reason in report.unreadable.items())
        lines += [f"**Could not read**, so not checked: {files}", ""]
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
    for unresolved in report.unresolved:
        rel = _relative(unresolved.path, consumer_root)
        message = f"{unresolved.raw}: unresolved variable; this ref was not checked"
        print(f"::warning file={rel},line={unresolved.lineno}::{message}" if on_actions else f"warning: {message}")
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
    for path, reason in report.unreadable.items():
        rel = _relative(path, consumer_root)
        message = f"cannot read {rel} ({reason}); its image refs were not checked"
        print(f"error: {message}", file=sys.stderr)
        if on_actions:
            print(f"::error file={rel}::{message}", file=sys.stderr)


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
