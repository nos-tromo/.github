"""Unit tests for scripts/validate_image_provenance.py.

Run with: uv run --with pytest python -m pytest tests/test_image_provenance.py -q

Image names, digests and keys here are invented placeholders; the real
policy entries are exercised against live registries by self-ci's
provenance-smoke job, not by these tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import validate_image_provenance as vip  # noqa: E402

D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64
D3 = "sha256:" + "c" * 64


# --- parse_image_ref -------------------------------------------------------


def test_docker_hub_short_name_gets_library_namespace():
    ref = vip.parse_image_ref("python:3.12-slim")
    assert (ref.registry, ref.repository, ref.tag, ref.digest) == ("docker.io", "library/python", "3.12-slim", None)


def test_namespaced_docker_hub_image():
    assert vip.parse_image_ref("example/tool:1.0").name == "docker.io/example/tool"


def test_explicit_registry_with_tag_and_digest():
    ref = vip.parse_image_ref(f"ghcr.io/example-org/tool:1.2.3@{D1}")
    assert ref.name == "ghcr.io/example-org/tool"
    assert ref.tag == "1.2.3"
    assert ref.digest == D1
    assert ref.pinned == f"ghcr.io/example-org/tool@{D1}"


def test_registry_with_port_is_not_mistaken_for_a_tag():
    ref = vip.parse_image_ref("localhost:5000/team/app:2")
    assert (ref.registry, ref.repository, ref.tag) == ("localhost:5000", "team/app", "2")


@pytest.mark.parametrize(
    ("value", "name"),
    [
        ("GHCR.IO/example-org/tool:1.0", "ghcr.io/example-org/tool"),
        ("ghcr.io:443/example-org/tool:1.0", "ghcr.io/example-org/tool"),
        ("Docker.IO:443/example/db:5", "docker.io/example/db"),
        ("INDEX.DOCKER.IO/example/db:5", "docker.io/example/db"),
        ("registry-1.docker.io:443/example/db:5", "docker.io/example/db"),
        ("ghcr.io:5000/example-org/tool:1.0", "ghcr.io:5000/example-org/tool"),
    ],
)
def test_registry_host_is_matched_in_any_case_and_without_its_default_port(value, name):
    assert vip.parse_image_ref(value).name == name


def test_digest_without_tag():
    ref = vip.parse_image_ref(f"busybox@{D1}")
    assert (ref.name, ref.tag, ref.digest) == ("docker.io/library/busybox", None, D1)


def test_malformed_digest_counts_as_unpinned():
    ref = vip.parse_image_ref("example/tool:1.0@sha256:abc")
    assert ref is not None
    assert ref.digest is None


@pytest.mark.parametrize("value", ["${BASE_IMAGE}", "app:${TAG}", "$IMAGE", ""])
def test_unresolved_or_empty_value_is_not_a_ref(value):
    assert vip.parse_image_ref(value) is None


# --- dockerfile_refs -------------------------------------------------------

DOCKERFILE = f"""\
# syntax=docker/dockerfile:1
ARG BASE=ghcr.io/example-org/tool:1.0@{D1}
FROM ${{BASE}} AS tool
FROM --platform=$BUILDPLATFORM docker.io/example/builder:2 AS build
COPY --from=tool /tool /bin/tool
COPY --from=docker.io/example/helper:3@{D2} \\
     /helper /bin/helper
FROM scratch
COPY --from=build /out /
"""


def test_dockerfile_refs_expand_args_and_skip_stages():
    assert vip.dockerfile_refs(DOCKERFILE) == [
        (3, f"ghcr.io/example-org/tool:1.0@{D1}"),
        (4, "docker.io/example/builder:2"),
        (6, f"docker.io/example/helper:3@{D2}"),
    ]


def test_dockerfile_arg_default_expansion():
    assert vip.dockerfile_refs("ARG TAG\nFROM example/app:${TAG:-1.0}\n") == [(2, "example/app:1.0")]


def test_dockerfile_unresolved_arg_is_kept_raw():
    assert vip.dockerfile_refs("ARG IMAGE\nFROM ${IMAGE}\n") == [(2, "${IMAGE}")]


def test_dockerfile_comments_are_ignored():
    assert vip.dockerfile_refs("# FROM example/commented:1\nFROM example/real:1\n") == [(2, "example/real:1")]


def test_dockerfile_keywords_are_case_insensitive():
    assert vip.dockerfile_refs("from example/app:1 as base\nfrom base\n") == [(1, "example/app:1")]


def test_dockerfile_copy_from_stage_index_is_skipped():
    assert vip.dockerfile_refs("FROM example/app:1\nFROM example/run:1\nCOPY --from=0 /a /a\n") == [
        (1, "example/app:1"),
        (2, "example/run:1"),
    ]


def test_dockerfile_escape_directive_stops_backslash_continuing_a_line():
    text = "# escape=`\nFROM example/base:1\nRUN dir C:\\\nFROM example/second:1\n"
    assert vip.dockerfile_refs(text) == [(2, "example/base:1"), (4, "example/second:1")]


def test_dockerfile_escape_directive_continues_lines_with_its_own_character():
    assert vip.dockerfile_refs("# escape=`\nFROM `\n    example/base:1\n") == [(2, "example/base:1")]


# --- compose_refs ----------------------------------------------------------

COMPOSE = f"""\
services:
  app:
    image: &app-image ghcr.io/example-org/app:1.0@{D1}  # pinned upstream
  sidecar:
    image: *app-image
  local:
    build: .
    image: "local-app:${{APP_VERSION:-latest}}"
  db:
    image: '${{DB_IMAGE}}'
  # image: example/commented:1
"""


def test_compose_refs_strip_anchors_quotes_comments_and_expand_defaults():
    assert vip.compose_refs(COMPOSE) == [
        (3, f"ghcr.io/example-org/app:1.0@{D1}"),
        (8, "local-app:latest"),
        (10, "${DB_IMAGE}"),
    ]


# --- target_files ----------------------------------------------------------


def test_target_files_find_dockerfiles_and_compose_files_but_skip_tool_dirs(tmp_path):
    for rel in [
        "Dockerfile",
        "build.Dockerfile",
        "docker/Dockerfile.backend",
        "docker/Dockerfile.backend.dockerignore",
        "docker/compose.yaml",
        "docker/compose.override.yaml",
        "docker-compose.yml",
        "docker/notes.md",
        ".devcontainer/Dockerfile",
        ".git/modules/x/Dockerfile",
        ".nos-tromo-github-ref/tests/fixtures/x/compose.yaml",
        ".venv/lib/pkg/Dockerfile",
        "frontend/node_modules/pkg/Dockerfile",
    ]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    found = [p.relative_to(tmp_path).as_posix() for p in vip.target_files(tmp_path)]
    assert found == [
        ".devcontainer/Dockerfile",
        "Dockerfile",
        "build.Dockerfile",
        "docker-compose.yml",
        "docker/Dockerfile.backend",
        "docker/compose.override.yaml",
        "docker/compose.yaml",
    ]


def test_target_files_skip_nested_checkouts(tmp_path):
    for rel in ["Dockerfile", "worktrees/feature/.git", "worktrees/feature/Dockerfile", "vendor/lib/.git/HEAD"]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (tmp_path / "vendor" / "lib" / "Dockerfile").write_text("")
    assert [p.relative_to(tmp_path).as_posix() for p in vip.target_files(tmp_path)] == ["Dockerfile"]


# --- load_policy -----------------------------------------------------------

POLICY = """\
[[image]]
name = "ghcr.io/example-org/tool"
method = "github-attestation"
repo = "example-org/tool"
signer-repo = "example-org/tool"
source-ref = "refs/heads/main"
github-hosted-only = true

[[image]]
name = "docker.io/example/db"
method = "cosign-key"
key = "keys/example.pub"
ignore-tlog = true

[[image]]
name = "docker.io/example/cache"
method = "cosign-keyless"
certificate-identity-regexp = '^https://github\\.com/example/cache/'
certificate-oidc-issuer = "https://token.actions.githubusercontent.com"
"""


def write_policy(directory: Path, text: str = POLICY) -> Path:
    (directory / "keys").mkdir(parents=True, exist_ok=True)
    (directory / "keys" / "example.pub").write_text("placeholder key\n")
    path = directory / "signers.toml"
    path.write_text(text)
    return path


def test_load_policy_indexes_entries_by_image_name(tmp_path):
    policy = vip.load_policy(write_policy(tmp_path))
    assert sorted(policy) == ["docker.io/example/cache", "docker.io/example/db", "ghcr.io/example-org/tool"]
    assert policy["docker.io/example/db"].key == tmp_path / "keys" / "example.pub"


def test_load_policy_rejects_an_unknown_method(tmp_path):
    bad = '[[image]]\nname = "docker.io/example/x"\nmethod = "pgp"\n'
    with pytest.raises(vip.PolicyError, match="method"):
        vip.load_policy(write_policy(tmp_path, bad))


def test_load_policy_rejects_a_missing_key_file(tmp_path):
    bad = '[[image]]\nname = "docker.io/example/x"\nmethod = "cosign-key"\nkey = "keys/absent.pub"\n'
    with pytest.raises(vip.PolicyError, match="key"):
        vip.load_policy(write_policy(tmp_path, bad))


def test_load_policy_requires_a_repo_for_github_attestation(tmp_path):
    bad = '[[image]]\nname = "ghcr.io/example-org/x"\nmethod = "github-attestation"\n'
    with pytest.raises(vip.PolicyError, match="repo"):
        vip.load_policy(write_policy(tmp_path, bad))


def test_load_policy_requires_identity_and_issuer_for_keyless(tmp_path):
    bad = '[[image]]\nname = "docker.io/example/x"\nmethod = "cosign-keyless"\n'
    with pytest.raises(vip.PolicyError, match="certificate"):
        vip.load_policy(write_policy(tmp_path, bad))


def test_load_policy_rejects_duplicate_names(tmp_path):
    entry = '[[image]]\nname = "docker.io/example/x"\nmethod = "cosign-key"\nkey = "keys/example.pub"\n'
    with pytest.raises(vip.PolicyError, match="duplicate"):
        vip.load_policy(write_policy(tmp_path, entry + entry))


def test_load_policy_rejects_a_file_that_is_not_utf8(tmp_path):
    path = write_policy(tmp_path)
    path.write_bytes(b'[[image]]\nname = "docker.io/example/x"  # caf\xe9\nmethod = "cosign-key"\n')
    with pytest.raises(vip.PolicyError, match="cannot read"):
        vip.load_policy(path)


# --- verify_command --------------------------------------------------------


def test_github_attestation_command(tmp_path):
    signer = vip.load_policy(write_policy(tmp_path))["ghcr.io/example-org/tool"]
    ref = vip.parse_image_ref(f"ghcr.io/example-org/tool:1.0@{D1}")
    assert vip.verify_command(signer, ref) == [
        "gh",
        "attestation",
        "verify",
        f"oci://ghcr.io/example-org/tool@{D1}",
        "--repo",
        "example-org/tool",
        "--signer-repo",
        "example-org/tool",
        "--source-ref",
        "refs/heads/main",
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]


def test_cosign_key_command_skips_the_tlog_only_when_configured(tmp_path):
    signer = vip.load_policy(write_policy(tmp_path))["docker.io/example/db"]
    ref = vip.parse_image_ref(f"example/db:5@{D2}")
    assert vip.verify_command(signer, ref) == [
        "cosign",
        "verify",
        "--key",
        str(tmp_path / "keys" / "example.pub"),
        "--insecure-ignore-tlog=true",
        f"docker.io/example/db@{D2}",
    ]


def test_cosign_keyless_command(tmp_path):
    signer = vip.load_policy(write_policy(tmp_path))["docker.io/example/cache"]
    ref = vip.parse_image_ref(f"example/cache:7@{D3}")
    assert vip.verify_command(signer, ref) == [
        "cosign",
        "verify",
        "--certificate-identity-regexp",
        "^https://github\\.com/example/cache/",
        "--certificate-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        f"docker.io/example/cache@{D3}",
    ]


# --- classify_failure ------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Error: HTTP 404: Not Found (https://api.github.com/repos/example-org/tool/attestations/x)", 1),
        ('Error: verifying with issuer "sigstore.dev"', 1),
        ("error during command execution: no signatures found", 1),
        ("error during command execution: no matching signatures: did not match", 1),
        ("something nobody anticipated", 1),
        ("Error: HTTP 503: Service Unavailable", 2),
        ("Error: HTTP 502: Bad Gateway", 2),
        ("dial tcp: lookup api.github.com: no such host", 2),
        ("Error: HTTP 403: API rate limit exceeded for installation", 2),
        ("To get started with GitHub CLI, please run:  gh auth login", 2),
        ("Get https://ghcr.io/v2/: context deadline exceeded", 2),
        ("GET https://index.docker.io/v2/: TOOMANYREQUESTS: rate limit", 2),
        ("read tcp 10.0.0.1:443: connection reset by peer", 2),
        # An identity mismatch is a policy failure even when the identity it
        # got (which the image's publisher controls) reads like an outage.
        (
            "error: no matching signatures: none of the expected identities matched what was in the "
            "certificate, got subjects [https://github.com/someone/timeout/.github/workflows/x.yml@refs/heads/main]",
            1,
        ),
        (
            "error: no matching signatures: got subjects "
            "[https://github.com/someone/toomanyrequests/.github/workflows/x.yml@refs/heads/main]",
            1,
        ),
        # gh's own identity checks: "expected <Field> to be <want>, got <have>".
        ("Error: expected SourceRepositoryRef to be refs/heads/main, got refs/heads/toomanyrequests", 1),
        (
            "Error: expected SourceRepositoryURI to be https://github.com/example-org/tool, "
            "got https://github.com/someone/toomanyrequests",
            1,
        ),
        (
            'Error: expected SAN value to match regex "^https://github.com/example-org/tool/", '
            'got "https://github.com/someone/toomanyrequests/.github/workflows/x.yml@refs/heads/main"',
            1,
        ),
        ("Error: expected RunnerEnvironment to be github-hosted, got self-hosted", 1),
        ("Get https://ghcr.io/v2/example-org/tool/manifests/1.0: unexpected EOF", 2),
    ],
)
def test_classify_failure(output, expected):
    assert vip.classify_failure(output) == expected


# --- check -----------------------------------------------------------------

GH_OK = json.dumps(
    [
        {
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "buildSignerURI": "https://github.com/example-org/tool/.github/workflows/release.yml@refs/heads/main"
                    }
                },
                "verifiedTimestamps": [{"type": "Tlog", "timestamp": "2026-01-02T03:04:05Z"}],
            }
        }
    ]
)
COSIGN_OK = json.dumps(
    [
        {
            "critical": {"image": {"docker-manifest-digest": "x"}},
            "optional": {
                "Subject": "https://github.com/example/cache/.github/workflows/release.yml@refs/tags/v1",
                "Bundle": {"Payload": {"integratedTime": 1767323045}},
            },
        }
    ]
)


class FakeRunner:
    """Accepts the listed digests; answers like gh/cosign do for anything else."""

    def __init__(self, accept=(), failure="Error: HTTP 404: Not Found", missing=False):
        self.accept = set(accept)
        self.failure = failure
        self.missing = missing
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.missing:
            raise FileNotFoundError(cmd[0])
        if any(d in arg for arg in cmd for d in self.accept):
            out = GH_OK if cmd[0] == "gh" else COSIGN_OK
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=self.failure)


def make_consumer(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def make_raw_consumer(root: Path, name: str, data: bytes) -> Path:
    """Write one file byte for byte, so line breaks and encoding stay exactly as given."""
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(data)
    return root


@pytest.fixture
def policy(tmp_path):
    return vip.load_policy(write_policy(tmp_path / "policy"))


def test_check_verifies_listed_images_and_reports_unsigned_ones(tmp_path, policy):
    consumer = make_consumer(
        tmp_path / "repo",
        {
            "docker/Dockerfile.backend": (
                f"FROM ghcr.io/example-org/tool:1.0@{D1} AS tool\nFROM docker.io/library/python:3.12@{D3}\n"
            ),
            "docker/compose.yaml": f"services:\n  db:\n    image: example/db:5@{D2}\n",
        },
    )
    report = vip.check(consumer, policy, runner=FakeRunner(accept={D1, D2}))
    assert report.exit_code == 0
    assert sorted(report.verified) == [f"docker.io/example/db@{D2}", f"ghcr.io/example-org/tool@{D1}"]
    assert report.unsigned == {"docker.io/library/python"}
    assert report.verified[f"ghcr.io/example-org/tool@{D1}"].signed_at == "2026-01-02"
    assert report.verified[f"docker.io/example/db@{D2}"].signed_at == "2026-01-02"


def test_check_verifies_each_pinned_digest_once(tmp_path, policy):
    line = f"FROM ghcr.io/example-org/tool:1.0@{D1}\n"
    consumer = make_consumer(tmp_path / "repo", {"a/Dockerfile": line, "b/Dockerfile": line})
    runner = FakeRunner(accept={D1})
    assert vip.check(consumer, policy, runner=runner).exit_code == 0
    assert len(runner.calls) == 1


def test_check_fails_on_a_digest_the_signer_did_not_sign(tmp_path, policy):
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": f"FROM ghcr.io/example-org/tool:1.0@{D1}\n"})
    report = vip.check(consumer, policy, runner=FakeRunner())
    assert report.exit_code == 1
    [failure] = report.failures
    assert (failure.path.name, failure.lineno) == ("Dockerfile", 1)
    assert "HTTP 404" in failure.reason


@pytest.mark.parametrize("host", ["GHCR.IO", "ghcr.io:443", "Ghcr.Io:443"])
def test_check_verifies_a_listed_image_whatever_its_host_spelling(tmp_path, policy, host):
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": f"FROM {host}/example-org/tool:1.0@{D1}\n"})
    runner = FakeRunner()
    report = vip.check(consumer, policy, runner=runner)
    assert report.exit_code == 1
    assert [cmd[3] for cmd in runner.calls] == [f"oci://ghcr.io/example-org/tool@{D1}"]


def test_check_fails_on_a_listed_image_without_a_digest_and_never_calls_the_tool(tmp_path, policy):
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": "FROM ghcr.io/example-org/tool:1.0\n"})
    runner = FakeRunner()
    report = vip.check(consumer, policy, runner=runner)
    assert report.exit_code == 1
    assert "not digest-pinned" in report.failures[0].reason
    assert runner.calls == []


def test_check_returns_2_when_the_verifier_is_not_installed(tmp_path, policy):
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": f"FROM ghcr.io/example-org/tool:1.0@{D1}\n"})
    report = vip.check(consumer, policy, runner=FakeRunner(missing=True))
    assert report.exit_code == 2
    assert "gh" in report.errors[0]


def test_check_returns_2_on_a_transient_service_error(tmp_path, policy):
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": f"FROM ghcr.io/example-org/tool:1.0@{D1}\n"})
    report = vip.check(consumer, policy, runner=FakeRunner(failure="Error: HTTP 503: Service Unavailable"))
    assert report.exit_code == 2


def test_check_returns_2_when_the_verifier_times_out(tmp_path, policy):
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": f"FROM ghcr.io/example-org/tool:1.0@{D1}\n"})

    def runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    report = vip.check(consumer, policy, runner=runner)
    assert report.exit_code == 2
    [error] = report.errors
    assert f"ghcr.io/example-org/tool@{D1}" in error
    assert "timed out" in error


def test_a_policy_failure_outranks_a_could_not_run_error(tmp_path, policy):
    consumer = make_consumer(
        tmp_path / "repo",
        {
            "Dockerfile": "FROM ghcr.io/example-org/tool:1.0\n",
            "compose.yaml": f"services:\n  db:\n    image: example/db:5@{D2}\n",
        },
    )
    report = vip.check(consumer, policy, runner=FakeRunner(failure="Error: HTTP 503: Service Unavailable"))
    assert report.exit_code == 1


def test_check_reports_a_file_it_cannot_decode_and_checks_the_rest(tmp_path, policy):
    consumer = make_consumer(tmp_path / "repo", {"compose.yaml": f"services:\n  db:\n    image: example/db:5@{D2}\n"})
    (consumer / "Dockerfile").write_bytes(b"# caf\xe9 au lait\nFROM example/app:1\n")
    report = vip.check(consumer, policy, runner=FakeRunner(accept={D2}))
    assert report.exit_code == 2
    assert report.unreadable == {consumer / "Dockerfile": "not valid UTF-8"}
    assert list(report.verified) == [f"docker.io/example/db@{D2}"]


def test_a_policy_failure_outranks_an_unreadable_file(tmp_path, policy):
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": "FROM ghcr.io/example-org/tool:1.0\n"})
    (consumer / "compose.yaml").write_bytes(b"services: {}  # caf\xe9\n")
    assert vip.check(consumer, policy, runner=FakeRunner()).exit_code == 1


def test_check_reads_a_dockerfile_that_starts_with_a_byte_order_mark(tmp_path, policy):
    dockerfile = b"\xef\xbb\xbf" + f"FROM ghcr.io/example-org/tool:1.0@{D1}\n".encode()
    consumer = make_raw_consumer(tmp_path / "repo", "Dockerfile", dockerfile)
    report = vip.check(consumer, policy, runner=FakeRunner(accept={D1}))
    assert report.exit_code == 0
    assert list(report.verified) == [f"ghcr.io/example-org/tool@{D1}"]


# BuildKit breaks Dockerfile lines at "\n" only and splits words at [\t\v\f\r ],
# so each of these still reads as FROM <image>.
@pytest.mark.parametrize("space", ["\f", "\v", "\r"])
def test_check_finds_an_arg_hidden_image_whatever_whitespace_follows_from(tmp_path, policy, space):
    dockerfile = f"ARG TOOL=tool:1.0@{D1}\nFROM{space}ghcr.io/example-org/${{TOOL}}\n"
    consumer = make_raw_consumer(tmp_path / "repo", "Dockerfile", dockerfile.encode())
    report = vip.check(consumer, policy, runner=FakeRunner())
    assert report.exit_code == 1
    assert list(report.results) == [f"ghcr.io/example-org/tool@{D1}"]


@pytest.mark.parametrize("space", ["\f", "\v", "\r"])
def test_check_verifies_a_listed_image_whatever_whitespace_follows_from(tmp_path, policy, space):
    dockerfile = f"FROM{space}ghcr.io/example-org/tool:1.0@{D1}\n"
    consumer = make_raw_consumer(tmp_path / "repo", "Dockerfile", dockerfile.encode())
    report = vip.check(consumer, policy, runner=FakeRunner(accept={D1}))
    assert report.exit_code == 0
    assert list(report.verified) == [f"ghcr.io/example-org/tool@{D1}"]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (
            "Dockerfile",
            f"FROM example/base:1\r\nCOPY \\\r\n  --from=ghcr.io/example-org/tool:1.0@{D1} \\\r\n  /tool /tool\r\n",
        ),
        ("compose.yaml", f"services:\r\n  tool:\r\n    image: ghcr.io/example-org/tool:1.0@{D1}\r\n"),
    ],
)
def test_check_reads_files_with_crlf_line_endings(tmp_path, policy, name, text):
    consumer = make_raw_consumer(tmp_path / "repo", name, text.encode())
    report = vip.check(consumer, policy, runner=FakeRunner(accept={D1}))
    assert report.exit_code == 0
    assert list(report.verified) == [f"ghcr.io/example-org/tool@{D1}"]


def test_check_counts_unresolved_refs_without_failing(tmp_path, policy):
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": "ARG IMAGE\nFROM ${IMAGE}\n"})
    report = vip.check(consumer, policy, runner=FakeRunner())
    assert report.exit_code == 0
    assert [(u.lineno, u.raw) for u in report.unresolved] == [(2, "${IMAGE}")]


@pytest.mark.parametrize(
    "line",
    [
        f"FROM ghcr.io/example-org/tool:${{TAG}}@{D1}",
        "FROM ghcr.io/example-org/${NAME}:1.0",
        "FROM docker.io/example/${DB}:5",
        "FROM GHCR.IO/example-org/tool:${TAG}",
        "FROM ghcr.io:443/example-org/${NAME}:1.0",
        "FROM ghcr.io:443${NAME}",
        "FROM registry-1.docker.io/example/db:${TAG}",
        "FROM Registry-1.Docker.IO:443/example/${DB}:5",
    ],
)
def test_check_fails_closed_when_a_variable_may_hide_a_listed_image(tmp_path, policy, line):
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": f"ARG TAG\nARG NAME\nARG DB\n{line}\n"})
    report = vip.check(consumer, policy, runner=FakeRunner(accept={D1}))
    assert report.exit_code == 1
    assert "unresolved variable" in report.failures[0].reason


@pytest.mark.parametrize(
    "files",
    [
        {"compose.yaml": f"services:\n  db:\n    image: ${{REGISTRY}}/example/db:5@{D2}\n"},
        {"Dockerfile": f"ARG REGISTRY\nFROM ${{REGISTRY}}/example-org/tool:1.0@{D1}\n"},
        {"Dockerfile": f"ARG REG\nFROM $REG/example-org/tool:1.0@{D1}\n"},
        {"Dockerfile": f"ARG MIRROR\nFROM ${{MIRROR}}example-org/tool:1.0@{D1}\n"},
    ],
)
def test_check_fails_closed_on_a_variable_prefix_before_a_listed_repository(tmp_path, policy, files):
    report = vip.check(make_consumer(tmp_path / "repo", files), policy, runner=FakeRunner(accept={D1, D2}))
    assert report.exit_code == 1
    assert "unresolved variable" in report.failures[0].reason


@pytest.mark.parametrize(
    "line",
    [
        f"image: ${{REGISTRY:?set the registry}}/example/db:5@{D2}",
        f"image: ${{REGISTRY:+mirror.example}}/example/db:5@{D2}",
        f"image: ${{REGISTRY?}}/example/db:5@{D2}",
    ],
)
def test_check_fails_closed_on_a_modifier_variable_before_a_listed_repository(tmp_path, policy, line):
    compose = f"services:\n  db:\n    {line}\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"compose.yaml": compose}), policy, FakeRunner(accept={D2}))
    assert report.exit_code == 1


@pytest.mark.parametrize(
    "line",
    [
        f"image: ${{A:+${{B}}}}/example/db:5@{D2}",  # nested variable
        "image: ${REG}/example/db${TAG}",  # variable right after the listed path
        "image: ${REG}/example/db${SEP}5",  # a variable that may be the tag separator
        "image: ${REG}/example/${NAME}:5",  # variable for the last path component
        f"image: ${{HOST}}.io/example-org/tool:1.0@{D1}",  # variable inside the registry host
    ],
)
def test_check_fails_closed_when_some_value_of_the_variables_names_a_listed_image(tmp_path, policy, line):
    compose = f"services:\n  db:\n    {line}\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"compose.yaml": compose}), policy, FakeRunner(accept={D1, D2}))
    assert report.exit_code == 1
    assert "unresolved variable" in report.failures[0].reason


@pytest.mark.parametrize("line", ["image: ${IMAGE}", "image: ${REGISTRY}/${IMAGE}:${TAG}"])
def test_a_ref_with_no_literal_name_left_only_warns(tmp_path, policy, line):
    compose = f"services:\n  db:\n    {line}\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"compose.yaml": compose}), policy, FakeRunner())
    assert report.exit_code == 0
    assert len(report.unresolved) == 1


def test_check_fails_closed_on_a_variable_before_a_listed_official_image_short_name(tmp_path):
    entry = '[[image]]\nname = "docker.io/library/exampleimg"\nmethod = "cosign-key"\nkey = "keys/example.pub"\n'
    policy = vip.load_policy(write_policy(tmp_path / "policy", entry))
    consumer = make_consumer(tmp_path / "repo", {"Dockerfile": f"ARG REG\nFROM ${{REG}}/exampleimg:1@{D1}\n"})
    report = vip.check(consumer, policy, runner=FakeRunner(accept={D1}))
    assert report.exit_code == 1


def test_a_variable_prefix_before_an_unlisted_repository_only_warns(tmp_path, policy):
    files = {
        "Dockerfile": (
            "ARG REG\nFROM ${REG}/example-org/tool-extras:1\nFROM ${REG}/someone/example-org/tool:1\n"
        )
    }
    report = vip.check(make_consumer(tmp_path / "repo", files), policy, runner=FakeRunner())
    assert report.exit_code == 0
    assert len(report.unresolved) == 2


def test_check_fails_closed_on_a_listed_image_in_a_compose_block_scalar(tmp_path, policy):
    compose = f"services:\n  tool:\n    image: >-\n      ghcr.io/example-org/tool:1.0@{D1}\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"compose.yaml": compose}), policy, FakeRunner(accept={D1}))
    assert report.exit_code == 1
    [failure] = report.failures
    assert failure.lineno == 4
    assert "not as a recognized image reference" in failure.reason


def test_check_fails_closed_on_a_listed_image_in_flow_style_yaml(tmp_path, policy):
    compose = f"services:\n  tool: {{image: 'ghcr.io/example-org/tool:1.0@{D1}'}}\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"compose.yaml": compose}), policy, FakeRunner(accept={D1}))
    assert report.exit_code == 1


@pytest.mark.parametrize(
    "compose",
    [
        f"services:\n  tool:\n    image: >-\n      GHCR.IO/example-org/tool:1.0@{D1}\n",
        f"services:\n  tool: {{image: 'ghcr.io:443/example-org/tool:1.0@{D1}'}}\n",
        f"services:\n  db:\n    image: >-\n      registry-1.docker.io/example/db:5@{D2}\n",
        f"services:\n  db: {{image: 'INDEX.DOCKER.IO:443/example/db:5@{D2}'}}\n",
    ],
)
def test_check_fails_closed_on_an_unparsed_listed_image_in_any_host_spelling(tmp_path, policy, compose):
    consumer = make_consumer(tmp_path / "repo", {"compose.yaml": compose})
    report = vip.check(consumer, policy, FakeRunner(accept={D1, D2}))
    assert report.exit_code == 1
    assert "not as a recognized image reference" in report.failures[0].reason


def test_check_fails_closed_on_a_listed_image_used_outside_from(tmp_path, policy):
    dockerfile = f"FROM example/base:1\nARG TOOL=ghcr.io/example-org/tool:1.0@{D1}\nRUN fetch $TOOL\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"Dockerfile": dockerfile}), policy, FakeRunner(accept={D1}))
    assert report.exit_code == 1
    assert report.failures[0].lineno == 2


def test_check_accepts_an_arg_default_that_feeds_a_from_line(tmp_path, policy):
    dockerfile = f"ARG TOOL=ghcr.io/example-org/tool:1.0@{D1}\n\nFROM ${{TOOL}}\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"Dockerfile": dockerfile}), policy, FakeRunner(accept={D1}))
    assert report.exit_code == 0


def test_check_accepts_a_listed_image_split_over_a_continuation(tmp_path, policy):
    dockerfile = f"FROM example/base:1\nCOPY \\\n  --from=ghcr.io/example-org/tool:1.0@{D1} \\\n  /tool /tool\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"Dockerfile": dockerfile}), policy, FakeRunner(accept={D1}))
    assert report.exit_code == 0
    assert len(report.verified) == 1


def test_check_fails_closed_on_a_listed_image_after_add_from(tmp_path, policy):
    # ADD has no --from flag (BuildKit: "unknown flag: --from"), so this is no image reference.
    dockerfile = f"FROM example/base:1\nADD --from=ghcr.io/example-org/tool:1.0@{D1} /tool /tool\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"Dockerfile": dockerfile}), policy, FakeRunner(accept={D1}))
    assert report.exit_code == 1
    assert "not as a recognized image reference" in report.failures[0].reason


def test_check_ignores_comments_that_mention_a_listed_image(tmp_path, policy):
    files = {
        "Dockerfile": f"# built from ghcr.io/example-org/tool\nFROM ghcr.io/example-org/tool:1.0@{D1}\n",
        "compose.yaml": f"services:\n  db:\n    image: example/db:5@{D2}  # mirrors ghcr.io/example-org/tool\n",
    }
    report = vip.check(make_consumer(tmp_path / "repo", files), policy, FakeRunner(accept={D1, D2}))
    assert report.exit_code == 0


def test_check_does_not_confuse_a_longer_name_with_a_listed_image(tmp_path, policy):
    dockerfile = "FROM ghcr.io/example-org/toolbox:1\nRUN echo ghcr.io/example-org/tool-extras\n"
    report = vip.check(make_consumer(tmp_path / "repo", {"Dockerfile": dockerfile}), policy, FakeRunner())
    assert report.exit_code == 0


def test_only_the_host_of_a_listed_name_matches_in_any_case(tmp_path, policy):
    compose = (
        "services:\n  app:\n    image: example/app:1\n    labels:\n"
        '      description: "Talks to Example/DB through GHCR.IO/Example-Org/Tool"\n'
    )
    report = vip.check(make_consumer(tmp_path / "repo", {"compose.yaml": compose}), policy, FakeRunner())
    assert report.exit_code == 0


def test_summary_lists_verified_images_failures_and_unsigned_images(tmp_path, policy):
    consumer = make_consumer(
        tmp_path / "repo",
        {
            "Dockerfile": f"FROM ghcr.io/example-org/tool:1.0@{D1}\nFROM python:3.12@{D3}\n",
            "compose.yaml": f"services:\n  db:\n    image: example/db:5@{D2}\n",
        },
    )
    summary = vip.render_summary(vip.check(consumer, policy, runner=FakeRunner(accept={D1})))
    assert "| `ghcr.io/example-org/tool` |" in summary
    assert "example-org/tool/.github/workflows/release.yml@refs/heads/main" in summary
    assert "| `docker.io/example/db` |" in summary
    assert "FAILED" in summary
    assert "`docker.io/library/python`" in summary


def cosign_key_output(digest: str, integrated_time: int | None) -> str:
    """Return what ``cosign verify --key`` prints: a key signature has no certificate subject.

    Without a transparency-log entry (``--insecure-ignore-tlog``) there is no
    bundle, so ``optional`` is null.
    """
    optional = None
    if integrated_time is not None:
        payload = {"body": "placeholder", "integratedTime": integrated_time, "logIndex": 1, "logID": "placeholder"}
        optional = {"Bundle": {"SignedEntryTimestamp": "placeholder", "Payload": payload}}
    critical = {
        "identity": {"docker-reference": "docker.io/example/db"},
        "image": {"docker-manifest-digest": digest},
        "type": "cosign container image signature",
    }
    return json.dumps([{"critical": critical, "optional": optional}])


@pytest.mark.parametrize(("integrated_time", "signed"), [(1767323045, "2026-01-02"), (None, "")])
def test_summary_row_for_a_cosign_key_signer_names_the_key(tmp_path, policy, integrated_time, signed):
    consumer = make_consumer(tmp_path / "repo", {"compose.yaml": f"services:\n  db:\n    image: example/db:5@{D2}\n"})
    output = cosign_key_output(D2, integrated_time)

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    summary = vip.render_summary(vip.check(consumer, policy, runner=runner))
    assert f"| `docker.io/example/db` | `sha256:bbbbbbbbbbbb` | verified | key example.pub | {signed} |" in summary


# --- main ------------------------------------------------------------------


def test_main_skips_a_repo_without_image_files(tmp_path, capsys):
    policy_path = write_policy(tmp_path / "policy")
    (tmp_path / "repo").mkdir()
    argv = ["--consumer-root", str(tmp_path / "repo"), "--signers", str(policy_path)]
    assert vip.main(argv, runner=FakeRunner()) == 0
    assert "skipping" in capsys.readouterr().out


def test_main_reports_ok_and_writes_the_step_summary(tmp_path, capsys, monkeypatch):
    policy_path = write_policy(tmp_path / "policy")
    make_consumer(tmp_path / "repo", {"Dockerfile": f"FROM ghcr.io/example-org/tool:1.0@{D1}\n"})
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    argv = ["--consumer-root", str(tmp_path / "repo"), "--signers", str(policy_path)]
    assert vip.main(argv, runner=FakeRunner(accept={D1})) == 0
    assert "image provenance check OK" in capsys.readouterr().out
    assert "ghcr.io/example-org/tool" in summary_file.read_text()


def test_main_annotates_failures_on_github_actions(tmp_path, capsys, monkeypatch):
    policy_path = write_policy(tmp_path / "policy")
    make_consumer(tmp_path / "repo", {"Dockerfile": f"FROM ghcr.io/example-org/tool:1.0@{D1}\n"})
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    argv = ["--consumer-root", str(tmp_path / "repo"), "--signers", str(policy_path)]
    assert vip.main(argv, runner=FakeRunner()) == 1
    err = capsys.readouterr().err
    assert "::error file=" in err
    assert ",line=1::" in err


def test_main_warns_about_unresolved_refs_on_github_actions(tmp_path, capsys, monkeypatch):
    policy_path = write_policy(tmp_path / "policy")
    make_consumer(tmp_path / "repo", {"Dockerfile": "ARG IMAGE\nFROM ${IMAGE}\n"})
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    argv = ["--consumer-root", str(tmp_path / "repo"), "--signers", str(policy_path)]
    assert vip.main(argv, runner=FakeRunner()) == 0
    assert "::warning file=Dockerfile,line=2::" in capsys.readouterr().out


def test_main_returns_2_on_an_unreadable_policy(tmp_path, capsys):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "Dockerfile").write_text("FROM example/app:1\n")
    argv = ["--consumer-root", str(tmp_path / "repo"), "--signers", str(tmp_path / "absent.toml")]
    assert vip.main(argv, runner=FakeRunner()) == 2


def test_main_returns_2_and_names_a_file_it_cannot_decode(tmp_path, capsys, monkeypatch):
    policy_path = write_policy(tmp_path / "policy")
    make_raw_consumer(tmp_path / "repo", "Dockerfile", b"# caf\xe9\nFROM example/app:1\n")
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    argv = ["--consumer-root", str(tmp_path / "repo"), "--signers", str(policy_path)]
    assert vip.main(argv, runner=FakeRunner()) == 2
    err = capsys.readouterr().err
    assert "Dockerfile" in err
    assert "not valid UTF-8" in err
    assert "::error file=Dockerfile::" in err
    summary = summary_file.read_text()
    assert "`Dockerfile`" in summary
    assert "No pinned image here" not in summary
