# nos-tromo/.github

Org-wide CI assets for the [nos-tromo](https://github.com/nos-tromo)
federation: three reusable GitHub Actions workflows and the canonical
strict-mode Python lint/type config that the Python-app workflow enforces.

## What's here

- [`.github/workflows/python-app-ci.yml`](.github/workflows/python-app-ci.yml)
  — reusable workflow for the four Python apps
  ([`chorus`](https://github.com/nos-tromo/chorus),
  [`docint`](https://github.com/nos-tromo/docint),
  [`Nextext`](https://github.com/nos-tromo/Nextext),
  [`translator`](https://github.com/nos-tromo/translator)). Runs
  pre-commit (ruff + pyrefly via the consumer's own `.pre-commit-config.yaml`),
  pytest across a Python-version matrix, and optional
  `docker compose build` and React/pnpm frontend jobs.
- [`.github/workflows/infra-validation.yml`](.github/workflows/infra-validation.yml)
  — reusable workflow for the infra repos
  ([`vllm-service`](https://github.com/nos-tromo/vllm-service),
  [`data-plane`](https://github.com/nos-tromo/data-plane),
  [`deploy`](https://github.com/nos-tromo/deploy)). yamllint, shellcheck,
  hadolint, and `docker compose config` validation (the last skipped when the
  caller passes no compose files, as `deploy` does).
- [`.github/workflows/node-lib-ci.yml`](.github/workflows/node-lib-ci.yml)
  — reusable workflow for the shared Node/TypeScript library
  ([`infra-ui`](https://github.com/nos-tromo/infra-ui), the `@infra/ui`
  design system). Runs pnpm lint, typecheck, test, and build, and optionally
  verifies a committed prebuilt output dir (`dist/`) is in sync with source.
- [`.github/workflows/claude.yml`](.github/workflows/claude.yml) — reusable
  workflow for **manual** `@claude` invocation (interactive/tag mode) in any
  consumer repo. Acts only when a human mentions `@claude` in an
  issue/PR/comment/review; deliberately has **no** automatic per-PR review.
- [`configs/python-strict/`](configs/python-strict/) — canonical ruff,
  pyrefly, and pre-commit version configs that Python-app consumers must
  mirror.
- [`scripts/validate_strict_config.py`](scripts/validate_strict_config.py)
  — alignment enforcer invoked from the `python-app-ci` workflow.
- [`configs/make-common/`](configs/make-common/), [`configs/bundle/`](configs/bundle/),
  [`configs/frontend-eslint/`](configs/frontend-eslint/) — other canonical files
  vendored verbatim into consumers, drift-checked by the matching
  `scripts/validate_*.py` (see [Vendored shared files](#vendored-shared-files)).
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

### Frontend: pin `@infra/ui` to a tarball URL, never `github:`

A `frontend` consuming the shared design system must reference it as a
**commit-SHA-pinned codeload tarball URL**, not the `github:` shorthand:

```jsonc
// frontend/package.json
"@infra/ui": "https://codeload.github.com/nos-tromo/infra-ui/tar.gz/<commit-sha>"  // correct
"@infra/ui": "github:nos-tromo/infra-ui#v0.2.1"                                    // wrong — breaks CI
```

A human `pnpm install` resolves the `github:` form to that same public HTTPS
tarball, so it looks fine locally. But when Dependabot regenerates
`pnpm-lock.yaml` for *any* frontend bump, it rewrites the entry to a
`git@github.com:` SSH resolution, which then fails both jobs: `frontend`
SSH-clones with no key (`Permission denied (publickey)`) and `docker` has no
`git` in the `node:*-alpine` builder (`pnpm: not found: git`). The pinned
tarball leaves no `github:` shorthand to rewrite and installs over HTTPS with
no key or git binary. Bump it by swapping the commit SHA (`git rev-list -n1
<tag>` in `infra-ui`); the lockfile `version`/`resolution` are unchanged, so
the re-lock diff is just the one `specifier` line.

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
| `compose-files`      | _(empty)_            | Space-separated `-f` arguments for `docker compose config`. Omit to skip the `compose-config` job (infra repos that own no compose, e.g. `deploy`). |
| `compose-profiles`   | _(empty)_            | Space-separated `--profile` arguments.                                             |
| `dockerfiles-glob`   | `docker/Dockerfile.*`| Glob for hadolint (fails only on `error`-level findings).                          |
| `shell-scripts-glob` | `scripts/*.sh`       | Glob for shellcheck.                                                               |

## Using the node-lib workflow

In the shared Node/TypeScript library (`infra-ui/.github/workflows/ci.yml`):

```yaml
name: CI
on:
  pull_request:
  push:
    branches: [main]

jobs:
  ci:
    uses: nos-tromo/.github/.github/workflows/node-lib-ci.yml@v3
    with:
      check-dist: true
```

Runs `pnpm install --frozen-lockfile`, then lint, typecheck, test, and build.
The pnpm version comes from the package's `packageManager` field. With
`check-dist: true`, a final step re-runs `pnpm build` and fails if the committed
output dir is no longer in sync with source — the guard for a library that ships
a prebuilt `dist/` in git, as `@infra/ui` does (every app frontend consumes it
as a commit-SHA-pinned tarball with no install-time rebuild).

Inputs:

| Input               | Default | Purpose                                                                 |
|---------------------|---------|-------------------------------------------------------------------------|
| `node-version`      | `20`    | Node version for the run.                                               |
| `working-directory` | `.`     | Package dir (where `package.json` + `pnpm-lock.yaml` live).             |
| `run-lint`          | `true`  | Run `pnpm lint`.                                                         |
| `run-typecheck`     | `true`  | Run `pnpm typecheck`.                                                    |
| `run-test`          | `true`  | Run `pnpm test`.                                                         |
| `run-build`         | `true`  | Run `pnpm build` (implied when `check-dist` is set).                     |
| `check-dist`        | `false` | After build, fail if the committed `dist-dir` drifts from a fresh build. |
| `dist-dir`          | `dist`  | Output dir checked by `check-dist`.                                      |

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
2. Add an org-level `CLAUDE_CODE_OAUTH_TOKEN` Actions secret scoped to the
   repos — the token `/install-github-app` provisions for a Claude Max/Pro
   subscription. `secrets: inherit` forwards it into the workflow. (Using the
   direct Claude API instead? Forward `ANTHROPIC_API_KEY` and swap the input —
   see the workflow header.)

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
2. **`[tool.pyrefly]` in `pyproject.toml`** ← [`pyrefly.toml`](configs/python-strict/pyrefly.toml).
3. **`rev:` for the ruff and pyrefly hooks in `.pre-commit-config.yaml`**
   ← [`precommit-versions.yaml`](configs/python-strict/precommit-versions.yaml).

To check alignment locally from a consumer repo:

```bash
python3 ../.github/scripts/validate_strict_config.py
```

(adjust the path; or pass `--consumer-root`). Exits 0 on alignment, 1 on
drift, with concrete entries on stderr.

A few intentional choices worth knowing:

- `ignore-missing-imports = ["*"]` in `pyrefly.toml` is load-bearing — without
  it, strict mode would fail on every untyped third-party import
  (Streamlit, the Neo4j driver, llama-index, etc.). Strict applies to
  first-party code; transitive untyped seams are out of scope.
- `ANN401` (forbid `Any`) is ignored in `ruff.toml` for the same reason:
  bridges to untyped libraries force `Any` constantly, and strict pyrefly
  is the actual rigor.
- The canonical regime is `preset = "strict"` — pyrefly's full strict checks.
  `uv run pyrefly init pyproject.toml --non-interactive` scaffolds a starting
  `[tool.pyrefly]` block but emits a laxer migration default, so set
  `preset = "strict"` and mirror the canonical values after scaffolding.

## Vendored shared files

Beyond the merged-in strict config, three files are **vendored verbatim** into
consumers and drift-checked by the reusable workflows (`python-app-ci` checks
all three; `infra-validation` the first two):

| Vendored file | Canonical source | Validator |
|---------------|------------------|-----------|
| `make/common.mk` | [`configs/make-common/`](configs/make-common/) | `scripts/validate_make_common.py` |
| `scripts/bundle-lib.sh` | [`configs/bundle/`](configs/bundle/) | `scripts/validate_bundle_lib.py` |
| `frontend/eslint.config.js` | [`configs/frontend-eslint/`](configs/frontend-eslint/) | `scripts/validate_eslint_config.py` |

Unlike the strict config (merged into `pyproject.toml` and compared
semantically), these are copied byte-for-byte — the check is an exact file
comparison, so re-vendor on change rather than hand-editing the copy.

**Required-ness is include-driven** — a vendored file is enforced only where the
repo opts in, so a bespoke repo is never forced to adopt:

- `make/common.mk` — required iff the `Makefile` has `include make/common.mk`.
- `scripts/bundle-lib.sh` — required iff `scripts/bundle_images.sh` sources it.
- `frontend/eslint.config.js` — checked only when present (frontends are optional).

So vendored-and-opted-in drift-checks; **missing-but-opted-in fails**; and
missing-and-not-opted-in is skipped (a legitimately bespoke repo, e.g.
`data-plane`, `open-webui`). A repo that adopts later becomes subject to the
check automatically — there is no exemption list to maintain.

## Versioning

Workflows are released as immutable minor tags (`v2.1`, `v2.2`, …, `v2.9`)
with a moving major alias (`v2`) that always points at the latest `v2.x`.
New consumers should pin the major alias (`@v2`) — as the examples above do —
so they track minor releases automatically. Pinning an exact minor (`@v2.9`)
also works: each consumer runs Dependabot's `github-actions` ecosystem, which
opens a bump PR when a newer tag ships.

Cutting a tag is the release mechanism, and it has **two** steps — the second
is easy to forget and silently strands `@v2` consumers on the old commit:

1. Tag the merge commit with the next minor —
   `git tag -a v2.10 -m "v2.10: …" && git push origin v2.10`
2. Move the major alias to the same commit —
   `git tag -f -a v2 -m "v2: …" && git push origin v2 --force`

Because the `python-app-ci` lint job validates each consumer against the
strict config that shipped with the tag it runs, a canonical-config change
and the consumers' mirrored-config updates must land together (see
[Strict-mode Python config](#strict-mode-python-config)) or the consumers'
lint jobs fail. The full tag list is on the
[tags page](https://github.com/nos-tromo/.github/tags).

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
   return 0; also exercises the `target-version` allowed-override and the
   `[tool.pyrefly]` mirror path) and a `drifted` fixture (must return non-zero).

When anything under `configs/python-strict/` changes, the aligned
fixture must be updated to mirror it — same drift signal real
consumers get, applied to this repo's own fixture.

To run the validator against a real consumer locally:

```bash
python3 scripts/validate_strict_config.py --consumer-root ../chorus
```

Requires Python 3.11+ (uses `tomllib`).
