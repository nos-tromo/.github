"""Unit tests for scripts/validate_profile_claims.py.

Run with: uv run --with pytest python -m pytest tests/test_profile_claims.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import validate_profile_claims as vpc  # noqa: E402

PAGE = """\
### Platform

| Repo | What it does |
|---|---|
| **[alpha-svc](https://example.invalid/demo-org/alpha-svc)** | Does the alpha thing. |
| **[Beta](https://example.invalid/demo-org/Beta)** | Does the beta thing. |

### Engineering glue

<!-- profile:no-diagram -->

| Repo | What it does |
|---|---|
| **[.config](https://example.invalid/demo-org/.config)** | Shared build glue. |

### Other public projects

- **[sidecar](https://example.invalid/demo-org/sidecar)** — not a member.
"""


def test_table_rows_yield_slugs_and_cells():
    rows = vpc.parse_table_rows(PAGE)
    assert [r.slug for r in rows] == ["alpha-svc", "Beta", ".config"]
    assert rows[0].cell.strip() == "Does the alpha thing."


def test_bullet_links_are_not_table_rows():
    slugs = [r.slug for r in vpc.parse_table_rows(PAGE)]
    assert "sidecar" not in slugs


def test_link_text_must_equal_slug():
    bad = "| **[alpha](https://example.invalid/demo-org/alpha-svc)** | x |\n"
    with pytest.raises(vpc.PageError, match="link text"):
        vpc.parse_table_rows(bad)


DIAGRAM_PAGE = """\
```mermaid
flowchart TB
  users(["Users"])

  subgraph Edge["Edge"]
    edge["edge-gw<br/>router - TLS"]
  end

  subgraph Apps["Applications"]
    a1["alpha-svc<br/>alpha thing"]
    b1["Beta<br/>beta thing"]
  end

  users -->|HTTPS :443| edge
  edge -->|net - :8443| b1
  edge -.->|scrapes| Apps
```
"""


def test_diagram_nodes_use_label_not_node_id():
    names = vpc.parse_diagram_repos(DIAGRAM_PAGE)
    assert names == {"edge-gw", "alpha-svc", "Beta"}


def test_nodes_without_a_label_separator_are_not_repos():
    names = vpc.parse_diagram_repos(DIAGRAM_PAGE)
    assert "Users" not in names
    assert "users" not in names


def test_subgraph_titles_are_not_repo_nodes():
    names = vpc.parse_diagram_repos(DIAGRAM_PAGE)
    assert "Edge" not in names and "Applications" not in names and "Apps" not in names


def test_page_with_no_mermaid_block_has_no_nodes():
    assert vpc.parse_diagram_repos("# just prose\n") == set()


def test_rows_under_a_no_diagram_marker_are_flagged():
    rows = {r.slug: r for r in vpc.parse_table_rows(PAGE)}
    assert rows[".config"].no_diagram is True
    assert rows["alpha-svc"].no_diagram is False
    assert rows["Beta"].no_diagram is False


def test_diagram_consistency_flags_a_missing_node():
    rows = vpc.parse_table_rows(PAGE)
    problems = vpc.check_diagram(rows, {"alpha-svc"})
    assert any("Beta" in p for p in problems)


def test_diagram_consistency_flags_a_glue_repo_drawn_as_a_node():
    rows = vpc.parse_table_rows(PAGE)
    problems = vpc.check_diagram(rows, {"alpha-svc", "Beta", ".config"})
    assert any(".config" in p for p in problems)


def test_diagram_consistency_is_silent_when_the_sets_line_up():
    rows = vpc.parse_table_rows(PAGE)
    assert vpc.check_diagram(rows, {"alpha-svc", "Beta"}) == []


def test_diagram_consistency_flags_a_node_that_is_not_a_member():
    rows = vpc.parse_table_rows(PAGE)
    problems = vpc.check_diagram(rows, {"alpha-svc", "Beta", "ghost-svc"})
    assert any("ghost-svc" in p for p in problems)


def _claims(body: str) -> list[vpc.Claim]:
    return vpc.load_claims(body)


def test_members_default_to_the_claims_own_repo():
    (claim,) = _claims(
        '[[claim]]\nid="a"\nrepo="alpha-svc"\nquote="alpha"\n'
        'file="c.yaml"\nmatches="^x:"\n'
    )
    assert claim.members == ("alpha-svc",)
    assert claim.kind == "matches"


def test_explicit_in_list_overrides_the_default_members():
    (claim,) = _claims(
        '[[claim]]\nid="a"\nrepo="alpha-svc"\nquote="alpha"\nin=["Beta",".config"]\n'
        'file="c.yaml"\nabsent="^x:"\n'
    )
    assert claim.members == ("Beta", ".config")


def test_unknown_key_is_rejected():
    with pytest.raises(vpc.ClaimsError, match="unknown key"):
        _claims('[[claim]]\nid="a"\nrepo="r"\nquote="q"\nfile="f"\nmatch="typo"\n')


def test_a_claim_needs_exactly_one_assertion():
    with pytest.raises(vpc.ClaimsError, match="exactly one"):
        _claims('[[claim]]\nid="a"\nrepo="r"\nquote="q"\nfile="f"\n')


def test_two_assertions_on_one_claim_are_rejected():
    with pytest.raises(vpc.ClaimsError, match="exactly one"):
        _claims(
            '[[claim]]\nid="a"\nrepo="r"\nquote="q"\nfile="f"\n'
            'matches="^x:"\nabsent="^y:"\n'
        )


def test_duplicate_claim_ids_are_rejected():
    with pytest.raises(vpc.ClaimsError, match="duplicate"):
        _claims(
            '[[claim]]\nid="a"\nrepo="r"\nquote="q"\nfile="f"\nmatches="^x:"\n'
            '[[claim]]\nid="a"\nrepo="r"\nquote="q2"\nfile="f"\nmatches="^y:"\n'
        )


def test_a_claim_must_carry_a_quote():
    with pytest.raises(vpc.ClaimsError, match="quote"):
        _claims('[[claim]]\nid="a"\nrepo="r"\nfile="f"\nmatches="^x:"\n')


def _member(tmp_path: Path, name: str, files: dict[str, str]) -> dict[str, Path]:
    root = tmp_path / name
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    return {name: root}


def _one(body: str) -> vpc.Claim:
    (claim,) = vpc.load_claims(body)
    return claim


def test_matches_holds_when_the_anchor_line_is_present(tmp_path):
    paths = _member(tmp_path, "m", {"c.yaml": "svc:\n  TEXT_MODEL: ${TEXT_MODEL:-x}\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\n'
        "file=\"c.yaml\"\nmatches='^\\s*TEXT_MODEL:'\n"
    )
    assert vpc.evaluate_claim(claim, paths) == []


def test_matches_fails_when_the_anchor_is_gone(tmp_path):
    paths = _member(tmp_path, "m", {"c.yaml": "svc:\n  OTHER: 1\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\n'
        "file=\"c.yaml\"\nmatches='^\\s*TEXT_MODEL:'\n"
    )
    assert any("0 hit" in p for p in vpc.evaluate_claim(claim, paths))


def test_a_comment_line_does_not_satisfy_an_anchor(tmp_path):
    paths = _member(tmp_path, "m", {"c.yaml": "# TEXT_MODEL: mentioned in prose\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\n'
        "file=\"c.yaml\"\nmatches='TEXT_MODEL'\n"
    )
    assert vpc.evaluate_claim(claim, paths) != []


def test_raw_opts_back_into_counting_comment_lines(tmp_path):
    paths = _member(tmp_path, "m", {"c.yaml": "# TEXT_MODEL: mentioned in prose\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\nraw=true\n'
        "file=\"c.yaml\"\nmatches='TEXT_MODEL'\n"
    )
    assert vpc.evaluate_claim(claim, paths) == []


def test_a_generic_anchor_hitting_many_lines_is_rejected(tmp_path):
    paths = _member(tmp_path, "m", {"c.yaml": "policy: a\npolicy: b\npolicy: c\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\nfile="c.yaml"\nmatches=\'policy:\'\n'
    )
    assert any("too generic" in p or "3 hit" in p for p in vpc.evaluate_claim(claim, paths))


def test_an_explicit_hits_range_allows_a_known_repeat(tmp_path):
    paths = _member(tmp_path, "m", {"c.yaml": "policy: a\npolicy: b\npolicy: c\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\nfile="c.yaml"\n'
        "matches='policy:'\nhits={min=3,max=3}\n"
    )
    assert vpc.evaluate_claim(claim, paths) == []


def test_absent_holds_when_nothing_matches(tmp_path):
    paths = _member(tmp_path, "m", {"c.yaml": "services:\n  a: 1\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\nfile="c.yaml"\nabsent=\'^\\s+ports:\'\n'
    )
    assert vpc.evaluate_claim(claim, paths) == []


def test_absent_fails_when_the_forbidden_line_appears(tmp_path):
    paths = _member(tmp_path, "m", {"c.yaml": "services:\n  a:\n    ports:\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\nfile="c.yaml"\nabsent=\'^\\s+ports:\'\n'
    )
    assert vpc.evaluate_claim(claim, paths) != []


def test_a_missing_file_fails_rather_than_vacuously_passing(tmp_path):
    paths = _member(tmp_path, "m", {"other.yaml": "x\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\nfile="gone.yaml"\nabsent=\'^x\'\n'
    )
    assert any("gone.yaml" in p for p in vpc.evaluate_claim(claim, paths))


def test_anchoring_a_claim_to_prose_is_refused(tmp_path):
    paths = _member(tmp_path, "m", {"README.md": "We support TOTP one day.\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\nfile="README.md"\nmatches=\'TOTP\'\n'
    )
    assert any("prose" in p for p in vpc.evaluate_claim(claim, paths))


def test_prose_anchor_is_allowed_when_explicitly_opted_in(tmp_path):
    paths = _member(tmp_path, "m", {"README.md": "We support TOTP one day.\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\nallow_prose=true\n'
        "file=\"README.md\"\nmatches='TOTP'\n"
    )
    assert vpc.evaluate_claim(claim, paths) == []


def test_json_len_checks_a_collection_size(tmp_path):
    paths = _member(tmp_path, "m", {"map.json": '{"a":1,"b":2,"c":3}'})
    ok = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\nfile="map.json"\n'
        "json_len={min=2,max=4}\n"
    )
    bad = _one(
        '[[claim]]\nid="b"\nrepo="m"\nquote="q"\nfile="map.json"\n'
        "json_len={min=40,max=60}\n"
    )
    assert vpc.evaluate_claim(ok, paths) == []
    assert any("3" in p for p in vpc.evaluate_claim(bad, paths))


def test_glob_count_counts_only_files_containing_a_pattern(tmp_path):
    paths = _member(
        tmp_path,
        "m",
        {
            "wf/a.yml": "on:\n  workflow_call:\n",
            "wf/b.yml": "on:\n  workflow_call:\n",
            "wf/c.yml": "# workflow_call: only in a comment\non:\n  push:\n",
        },
    )
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\n'
        "glob_count={pattern=\"wf/*.yml\",containing='^\\s*workflow_call:',eq=2}\n"
    )
    assert vpc.evaluate_claim(claim, paths) == []


def test_glob_matching_no_files_fails_instead_of_counting_zero(tmp_path):
    paths = _member(tmp_path, "m", {"wf/a.yml": "x\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\n'
        'glob_count={pattern="gone/*.yml",eq=0}\n'
    )
    assert any("matched no files" in p for p in vpc.evaluate_claim(claim, paths))


def test_exists_requires_every_listed_path(tmp_path):
    paths = _member(tmp_path, "m", {"src/Button.tsx": "x\n", "src/Card.tsx": "x\n"})
    ok = _one(
        '[[claim]]\nid="a"\nrepo="m"\nquote="q"\n'
        'exists=["src/Button.tsx","src/Card.tsx"]\n'
    )
    bad = _one(
        '[[claim]]\nid="b"\nrepo="m"\nquote="q"\n'
        'exists=["src/Button.tsx","src/Missing.tsx"]\n'
    )
    assert vpc.evaluate_claim(ok, paths) == []
    assert any("Missing.tsx" in p for p in vpc.evaluate_claim(bad, paths))


def test_a_fanned_out_claim_must_hold_in_every_member(tmp_path):
    paths = _member(tmp_path, "one", {"c.yaml": "services:\n"})
    paths |= _member(tmp_path, "two", {"c.yaml": "services:\n  ports:\n"})
    claim = _one(
        '[[claim]]\nid="a"\nrepo="one"\nquote="q"\nin=["one","two"]\n'
        "file=\"c.yaml\"\nabsent='^\\s+ports:'\n"
    )
    problems = vpc.evaluate_claim(claim, paths)
    assert any("two" in p for p in problems)
    assert not any(p.startswith("one:") for p in problems)


MINI_PAGE = """\
### Platform

| Repo | What it does |
|---|---|
| **[alpha-svc](https://example.invalid/demo-org/alpha-svc)** | Reads the alpha config. |

<!-- profile:no-diagram -->

| Repo | What it does |
|---|---|
| **[.config](https://example.invalid/demo-org/.config)** | Shared glue. |

```mermaid
flowchart TB
  a1["alpha-svc<br/>alpha thing"]
```
"""

COVERING = (
    '[[claim]]\nid="alpha-cfg"\nrepo="alpha-svc"\nquote="Reads the alpha config"\n'
    "file=\"c.yaml\"\nmatches='^alpha:'\n"
    '[[claim]]\nid="glue"\nrepo=".config"\nquote="Shared glue"\n'
    "file=\"c.yaml\"\nmatches='^alpha:'\n"
)


def _two_members(tmp_path: Path) -> dict[str, Path]:
    paths = _member(tmp_path, "alpha-svc", {"c.yaml": "alpha: 1\n"})
    paths |= _member(tmp_path, ".config", {"c.yaml": "alpha: 1\n"})
    return paths


def test_a_fully_covered_page_passes(tmp_path):
    report = vpc.audit(MINI_PAGE, vpc.load_claims(COVERING), _two_members(tmp_path))
    assert report.exit_code == 0
    assert report.drift == [] and report.errors == []


def test_a_table_repo_with_no_claim_is_reported_as_uncovered(tmp_path):
    only_one = COVERING.split("[[claim]]\nid=\"glue\"")[0]
    report = vpc.audit(MINI_PAGE, vpc.load_claims(only_one), _two_members(tmp_path))
    assert report.exit_code == 1
    assert any(".config" in d and "no claim" in d for d in report.drift)


def test_a_quote_that_left_the_row_is_drift(tmp_path):
    stale = COVERING.replace("Reads the alpha config", "Reads the beta config")
    report = vpc.audit(MINI_PAGE, vpc.load_claims(stale), _two_members(tmp_path))
    assert report.exit_code == 1
    assert any("alpha-cfg" in d and "quote" in d for d in report.drift)


def test_a_page_level_quote_is_checked_against_the_whole_page(tmp_path):
    claims = vpc.load_claims(
        COVERING
        + '[[claim]]\nid="intro"\nquote="nowhere on this page"\n'
        "glob_count={pattern=\"c.yaml\",min=1}\n"
    )
    report = vpc.audit(MINI_PAGE, claims, _two_members(tmp_path))
    assert any("intro" in d and "quote" in d for d in report.drift)


def test_a_claim_naming_an_unknown_repo_is_an_error(tmp_path):
    claims = vpc.load_claims(
        COVERING
        + '[[claim]]\nid="ghost"\nrepo="ghost-svc"\nquote="Shared glue"\n'
        "file=\"c.yaml\"\nmatches='^alpha:'\n"
    )
    report = vpc.audit(MINI_PAGE, claims, _two_members(tmp_path))
    assert report.exit_code == 2
    assert any("ghost-svc" in e for e in report.errors)


def test_a_missing_checkout_is_an_environment_error_not_drift(tmp_path):
    paths = _member(tmp_path, "alpha-svc", {"c.yaml": "alpha: 1\n"})
    report = vpc.audit(MINI_PAGE, vpc.load_claims(COVERING), paths)
    assert report.exit_code == 2


def test_only_present_skips_members_that_were_not_checked_out(tmp_path):
    paths = _member(tmp_path, "alpha-svc", {"c.yaml": "alpha: 1\n"})
    report = vpc.audit(
        MINI_PAGE, vpc.load_claims(COVERING), paths, only_present=True
    )
    assert report.exit_code == 0
    assert any(".config" in s for s in report.skipped)


import subprocess  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "validate_profile_claims.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _run(fixture: str, *extra: str) -> subprocess.CompletedProcess[str]:
    root = FIXTURES / fixture
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--profile", str(root / "profile" / "README.md"),
            "--claims", str(root / "profile" / "claims.toml"),
            "--members-root", str(root / "members"),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_exits_zero_on_the_aligned_fixture():
    done = _run("profile-aligned")
    assert done.returncode == 0, done.stdout + done.stderr


def test_cli_exits_one_on_the_drifted_fixture():
    done = _run("profile-drifted")
    assert done.returncode == 1, done.stdout + done.stderr


def test_cli_exits_one_when_a_row_has_no_claim():
    done = _run("profile-uncovered")
    assert done.returncode == 1, done.stdout + done.stderr


def test_cli_exits_two_when_a_member_checkout_is_missing():
    done = _run("profile-member-absent")
    assert done.returncode == 2, done.stdout + done.stderr


def test_only_present_downgrades_a_missing_checkout_to_a_skip():
    done = _run("profile-member-absent", "--only-present")
    assert done.returncode == 0, done.stdout + done.stderr


def test_list_repos_prints_every_table_slug():
    done = _run("profile-aligned", "--list-repos")
    assert done.returncode == 0
    assert done.stdout.split() == ["alpha-svc", "Beta", ".config"]


def test_cli_exits_two_on_an_unreadable_claims_file(tmp_path):
    done = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--profile", str(FIXTURES / "profile-aligned" / "profile" / "README.md"),
            "--claims", str(tmp_path / "nope.toml"),
            "--members-root", str(tmp_path),
        ],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 2
