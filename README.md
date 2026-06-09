# nos-tromo/.github

Org-wide CI assets for the [nos-tromo](https://github.com/nos-tromo)
federation: two reusable GitHub Actions workflows and the canonical
strict-mode Python lint/type config that the workflows enforce.

## What's here

- [`.github/workflows/python-app-ci.yml`](.github/workflows/python-app-ci.yml)
  — reusable workflow for the four Python apps
  ([`chorus`](https://github.com/nos-tromo/chorus),
  [`docint`](https://github.com/nos-tromo/docint),
  [`Nextext`](https://github.com/nos-tromo/Nextext),
  [`translator`](https://github.com/nos-tromo/translator)). Runs
  pre-commit (ruff + mypy via the consumer's own `.pre-commit-config.yaml`),
  pytest across a Python-version matrix, and optional
  `docker compose build` and React/pnpm frontend jobs.
- [`.github/workflows/infra-validation.yml`](.github/workflows/infra-validation.yml)
  — reusable workflow for the infra repos
  ([`vllm-service`](https://github.com/nos-tromo/vllm-service),
  [`data-plane`](https://github.com/nos-tromo/data-plane)). yamllint,
  shellcheck, hadolint, and `docker compose config` validation.
- [`.github/workflows/claude.yml`](.github/workflows/claude.yml) — reusable
  workflow for **manual** `@claude` invocation (interactive/tag mode) in any
  consumer repo. Acts only when a human mentions `@claude` in an
  issue/PR/comment/review; deliberately has **no** automatic per-PR review.
- [`configs/python-strict/`](configs/python-strict/) — canonical ruff,
  mypy, and pre-commit version configs that Python-app consumers must
  mirror.
- [`scripts/validate_strict_config.py`](scripts/validate_strict_config.py)
  — alignment enforcer invoked from the `python-app-ci` workflow.
- [`.github/dependabot.yml`](.github/dependabot.yml) — org-default
  dependabot template (also runs on this repo for `github-actions`
  updates).

## Using the Python-app workflow

In a Python-app consumer (e.g. `chorus/.github/workflows/ci.yml`):

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]

jobs:
  ci:
    uses: nos-tromo/.github/.github/workflows/python-app-ci.yml@v2
    with:
      python-versions: '["3.12", "3.13"]'
```

The doubled `.github/.github/` is correct — the repo is *named* `.github`.

Common inputs (full schema at the top of the workflow file):

| Input                     | Default                                              | Purpose                                                                                                            |
|---------------------------|------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| `python-versions`         | _(required)_                                         | JSON list. Lint runs against the first version; tests run against all.                                             |
| `uv-sync-args`            | `--frozen --group dev`                               | Override for repos with extras (e.g. `--frozen --group dev --extra cuda`).                                         |
| `docker-build`            | `false`                                              | Set `true` to validate `docker compose build`. The job stubs `inference-net`, `data-net`, and a placeholder `.env`. |
| `docker-compose-files`    | `-f docker/compose.yaml -f docker/compose.override.yaml` | Compose file selection for `docker compose build`.                                                                 |
| `docker-compose-profiles` | _(empty)_                                            | E.g. `--profile cpu`. Required where compose gates services behind a profile.                                      |
| `frontend-build`          | `false`                                              | Set `true` for repos with a React/pnpm frontend (e.g. `docint`).                                                   |
| `frontend-dir`            | `frontend`                                           | Path to the frontend project.                                                                                      |
| `test-env`                | _(empty)_                                            | Multiline `KEY=VALUE` block for apps whose imports require env at module scope (e.g. `translator`'s `OPENAI_API_BASE`). |
| `pytest-args`             | _(empty)_                                            | Extra args passed verbatim to `pytest`.                                                                            |

## Using the infra-validation workflow

In an infra-only consumer (`vllm-service`, `data-plane`):

```yaml
name: ci
on:
  pull_request:
  push:
    branches: [main]

jobs:
  ci:
    uses: nos-tromo/.github/.github/workflows/infra-validation.yml@v2
    with:
      compose-files: "-f docker/compose.yaml -f docker/compose.override.yaml"
      compose-profiles: "--profile cpu --profile cuda"
```

Inputs:

| Input                | Default              | Purpose                                                                            |
|----------------------|----------------------|------------------------------------------------------------------------------------|
| `compose-files`      | _(required)_         | Space-separated `-f` arguments for `docker compose config`.                        |
| `compose-profiles`   | _(empty)_            | Space-separated `--profile` arguments.                                             |
| `dockerfiles-glob`   | `docker/Dockerfile.*`| Glob for hadolint (fails only on `error`-level findings).                          |
| `shell-scripts-glob` | `scripts/*.sh`       | Glob for shellcheck.                                                               |

## Using the Claude mention workflow

Turns on Claude's `@claude` mention in a consumer repo — **manual only, no
automatic reviews**. The comment/issue triggers must live in the caller (a
reusable workflow can't declare triggers that fire in another repo), so drop
this into a consumer as `.github/workflows/claude.yml`:

```yaml
name: claude
on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]
  issues:
    types: [opened, assigned]
  pull_request_review:
    types: [submitted]

# The caller must grant these. id-token: write in particular is NOT covered by
# a repo's default token permissions, so it must be requested here or the
# Claude App's OIDC token exchange is capped to none and Claude can't act.
permissions:
  contents: write
  pull-requests: write
  issues: write
  id-token: write
  actions: read

jobs:
  claude:
    uses: nos-tromo/.github/.github/workflows/claude.yml@v2
    secrets: inherit
```

One-time prerequisites (org-wide):

1. Install the [Claude GitHub App](https://github.com/apps/claude).
2. Add an org-level `ANTHROPIC_API_KEY` Actions secret scoped to the repos;
   `secrets: inherit` forwards it into the workflow. (Subscription auth:
   forward `CLAUDE_CODE_OAUTH_TOKEN` and swap the input — see the workflow
   header.)

Optional inputs:

| Input            | Default   | Purpose                                                         |
|------------------|-----------|-----------------------------------------------------------------|
| `trigger_phrase` | `@claude` | Phrase that summons Claude in an issue/PR/comment.              |
| `claude_args`    | _(empty)_ | Verbatim Claude Code CLI args, e.g. `--model … --max-turns 10`. |

There is intentionally **no automatic per-PR review**: the workflow exposes no
`prompt` input and wires no `pull_request` trigger, so `claude-code-action@v1`
stays in interactive mode. Automatic review would be a separate opt-in
workflow.

## Strict-mode Python config

The `lint` job in `python-app-ci.yml` checks out **this repo at the same
ref the workflow is running at** and diffs the consumer's `pyproject.toml`
and `.pre-commit-config.yaml` against [`configs/python-strict/`](configs/python-strict/).
Any drift fails CI.

The guarantee: a consumer pinned to tag `vN` is always validated against
the canonical config that shipped with `vN`.

Consumers must mirror, exactly:

1. **`[tool.ruff]` in `pyproject.toml`** ← [`ruff.toml`](configs/python-strict/ruff.toml).
   The only key a consumer may override is `target-version` (each repo
   has a different Python floor).
2. **`[tool.mypy]` in `pyproject.toml`** ← [`mypy.ini`](configs/python-strict/mypy.ini).
3. **`rev:` for the ruff and mypy hooks in `.pre-commit-config.yaml`**
   ← [`precommit-versions.yaml`](configs/python-strict/precommit-versions.yaml).

To check alignment locally from a consumer repo:

```bash
python3 ../.github/scripts/validate_strict_config.py
```

(adjust the path; or pass `--consumer-root`). Exits 0 on alignment, 1 on
drift, with concrete entries on stderr.

A few intentional choices worth knowing:

- `ignore_missing_imports = true` in `mypy.ini` is load-bearing — without
  it, strict mode would fail on every untyped third-party import
  (Streamlit, the Neo4j driver, llama-index, etc.). Strict applies to
  first-party code; transitive untyped seams are out of scope.
- `ANN401` (forbid `Any`) is ignored in `ruff.toml` for the same reason:
  bridges to untyped libraries force `Any` constantly, and strict mypy
  is the actual rigor.

## Versioning

Consumers reference workflows by tag (`@v2`). Cutting a new tag is the
release mechanism — when the canonical config changes, the tag bump and
the consumer mirrored-config update must land together, or the lint job
in the consumer will fail.

Existing tags: `v1`, `v1.0.0`, `v2`. New consumers should pin to the
latest.

## Working in this repo

A self-CI workflow ([`.github/workflows/self-ci.yml`](.github/workflows/self-ci.yml))
runs on every PR and push to `main`. It does two things:

1. **Lints `scripts/`** with `ruff check` and `ruff format --check`
   using the canonical strict config and the same ruff version every
   consumer gets (pinned in
   [`precommit-versions.yaml`](configs/python-strict/precommit-versions.yaml)).
   The validator that enforces strict mode must itself pass strict mode.
2. **Smoke-tests the validator** against fixtures in
   [`tests/fixtures/`](tests/fixtures/): an `aligned` fixture (must
   return 0; also exercises the `target-version` allowed-override) and
   a `drifted` fixture (must return non-zero).

When anything under `configs/python-strict/` changes, the aligned
fixture must be updated to mirror it — same drift signal real
consumers get, applied to this repo's own fixture.

To run the validator against a real consumer locally:

```bash
python3 scripts/validate_strict_config.py --consumer-root ../chorus
```

Requires Python 3.11+ (uses `tomllib`).
