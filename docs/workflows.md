# Reusable workflows

Reference for the five reusable workflows this repo ships. The
[top-level README](../README.md) carries the minimal caller snippet for each
one; this file carries the input schemas, prerequisites and options around
them.

`.github/workflows/` holds a sixth file, `self-ci.yml`. It is **not** callable —
it has no `workflow_call` trigger, only `push`/`pull_request` — because it is
this repo's own CI. See [maintaining.md](maintaining.md).

Two things apply to every caller:

- The doubled `.github/.github/` in a `uses:` path is correct — the repo is
  *named* `.github`.
- Every `uses:` ref must be pinned to a full 40-character commit SHA with the
  version in a trailing comment. See
  [pinning.md](pinning.md#action-refs) for why, how to resolve a tag's SHA,
  and the check that enforces it.

## python-app-ci

Four jobs for the Python consumers — the four apps (`chorus`, `docint`,
`Nextext`, `translator`) and `vllm-service`, which calls it lint-only with
`run-tests: false`:

- **`lint`** — always runs. Checks this repo out at the ref the caller pinned,
  then runs all six validators against the consumer
  ([strict-python.md](strict-python.md), [vendored-files.md](vendored-files.md),
  [pinning.md](pinning.md)): `validate_strict_config`, `validate_make_common`,
  `validate_bundle_lib`, `validate_eslint_config`, `validate_action_pins`,
  `validate_infra_ui_pin`. Then `uv sync` and `pre-commit run --all-files`
  (ruff + pyrefly, from the consumer's own `.pre-commit-config.yaml`).
- **`test`** — `needs: lint`; pytest across the `python-versions` matrix.
  Gated on `run-tests`.
- **`frontend`** — `needs: lint`; gated on `frontend-build`. Runs
  `pnpm install --frozen-lockfile`, then `pnpm test` and `pnpm build` (and
  `pnpm lint` when `frontend-lint` is set).
- **`docker`** — `needs: test`; gated on `docker-build`.

Inputs — the complete `workflow_call` schema, in declaration order. Only
`python-versions` is required:

| Input                     | Type      | Default                                                  | Purpose                                                                                                                                      |
|---------------------------|-----------|----------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------|
| `python-versions`         | `string`  | _(required)_                                             | JSON list, e.g. `'["3.11", "3.12"]'`. Lint runs against the first element; the test matrix runs against all.                                  |
| `uv-sync-args`            | `string`  | `--locked --group dev`                                   | Args passed verbatim to `uv sync` in the lint and test jobs. Override for repos with extras (e.g. `--locked --group dev --extra cpu`).        |
| `docker-build`            | `boolean` | `false`                                                  | Set `true` to validate `docker compose build`. The job stubs `inference-net`, `data-net`, and a placeholder `.env` first.                     |
| `docker-compose-files`    | `string`  | `-f docker/compose.yaml -f docker/compose.override.yaml` | Compose file selection for `docker compose build`.                                                                                           |
| `docker-compose-profiles` | `string`  | _(empty)_                                                | E.g. `--profile cpu`. Required where compose gates services behind a profile.                                                                 |
| `free-disk-space`         | `boolean` | `true`                                                   | Free runner disk before the docker job (large ML images need it). No effect unless `docker-build` is set.                                     |
| `frontend-build`          | `boolean` | `false`                                                  | Set `true` for repos with a React/pnpm frontend (e.g. `docint`). Note the job runs `pnpm test` as well as `pnpm build`.                       |
| `frontend-dir`            | `string`  | `frontend`                                               | Path to the frontend project — also where the lint job's `eslint.config.js` and `@infra/ui` pin checks look.                                  |
| `node-version`            | `string`  | `20`                                                     | Node version for the `frontend` job.                                                                                                         |
| `pnpm-version`            | `string`  | `9.12.0`                                                 | pnpm version for the `frontend` job, passed to `pnpm/action-setup`. (Unlike `node-lib-ci`, which reads `packageManager` instead.)             |
| `run-tests`               | `boolean` | `true`                                                   | Run the pytest matrix. `false` gives a lint-only run for repos with no suite, as `vllm-service` does — see the gotcha below.                  |
| `pytest-args`             | `string`  | _(empty)_                                                | Extra args passed verbatim to `pytest`.                                                                                                      |
| `test-env`                | `string`  | _(empty)_                                                | Multiline `KEY=VALUE` block appended to `$GITHUB_ENV` before pytest, for apps whose imports require env at module scope (e.g. `translator`'s `OPENAI_API_BASE`). |
| `frontend-lint`           | `boolean` | `false`                                                  | Also run `pnpm lint` in the `frontend` job. Enable once the repo vendors the canonical `eslint.config.js` and passes lint.                    |

**`run-tests: false` also disables the docker job.** `docker` declares
`needs: test`, and a skipped dependency skips the dependent — so
`docker-build: true` is only meaningful with `run-tests` left at `true`.

A consumer with a `frontend/` must also pin `@infra/ui` correctly, or both the
`frontend` and `docker` jobs fail — see
[pinning.md](pinning.md#infra-ui-tarball-pins).

## infra-validation

Five jobs for the infra repos (`vllm-service`, `data-plane`, `obs-plane`,
`edge-plane`, `open-webui-service`, `deploy`):

- **`yamllint`** — `yamllint -d "{extends: relaxed, …}"` over the whole repo.
- **`shellcheck`** — over `shell-scripts-glob`; skips when nothing matches.
- **`hadolint`** — over `dockerfiles-glob`; skips when nothing matches.
- **`compose-config`** — `docker compose config --quiet`, after stubbing the
  external networks/volumes and a placeholder `.env`. Skipped when
  `compose-files` is empty.
- **`make-common`** — the vendored-file and action-pin drift checks, run the
  same ref-locked way as `python-app-ci`'s lint job:
  `validate_make_common`, `validate_bundle_lib`, `validate_action_pins`. Each
  skips a repo that has not opted in ([vendored-files.md](vendored-files.md)).

Inputs — the complete `workflow_call` schema. None is required:

| Input                | Type     | Default              | Purpose                                                                            |
|----------------------|----------|----------------------|------------------------------------------------------------------------------------|
| `compose-files`      | `string` | _(empty)_            | Space-separated `-f` arguments for `docker compose config`. Omit to skip the `compose-config` job (infra repos that own no compose, e.g. `deploy`). |
| `compose-profiles`   | `string` | _(empty)_            | Space-separated `--profile` arguments.                                             |
| `dockerfiles-glob`   | `string` | `docker/Dockerfile.*`| Glob for hadolint (fails only on `error`-level findings).                          |
| `shell-scripts-glob` | `string` | `scripts/*.sh`       | Glob for shellcheck.                                                               |

## node-lib-ci

Two jobs, for the shared Node/TypeScript library (`infra-ui`):

- **`ci`** — `pnpm install --frozen-lockfile`, then lint, typecheck, test and
  build, each behind its own toggle. The pnpm version comes from the package's
  `packageManager` field, not an input. With `check-dist: true`, a final step
  fails if the `pnpm build` it just ran left the committed output dir dirty —
  the guard for a library that ships a prebuilt `dist/` in git, as `@infra/ui`
  does (every app frontend consumes it as a commit-SHA-pinned tarball with no
  install-time rebuild).
- **`action-pins`** — `validate_action_pins.py` against the consumer's own
  workflows, ref-locked to the pinned hub revision
  ([pinning.md](pinning.md#action-refs)).

Inputs — the complete `workflow_call` schema. None is required:

| Input               | Type      | Default | Purpose                                                                 |
|---------------------|-----------|---------|-------------------------------------------------------------------------|
| `node-version`      | `string`  | `20`    | Node version for the run.                                               |
| `working-directory` | `string`  | `.`     | Package dir (where `package.json` + `pnpm-lock.yaml` live).             |
| `run-lint`          | `boolean` | `true`  | Run `pnpm lint`.                                                         |
| `run-typecheck`     | `boolean` | `true`  | Run `pnpm typecheck`.                                                    |
| `run-test`          | `boolean` | `true`  | Run `pnpm test`.                                                         |
| `run-build`         | `boolean` | `true`  | Run `pnpm build` (implied when `check-dist` is set).                     |
| `check-dist`        | `boolean` | `false` | After build, fail if the committed `dist-dir` drifts from a fresh build. |
| `dist-dir`          | `string`  | `dist`  | Output dir checked by `check-dist`.                                      |

## claude

Manual `@claude` invocation in a consumer repo. The caller snippet (triggers and
the `permissions:` block it must grant) is in the
[README](../README.md#claude-mentions).

One-time prerequisites (org-wide):

1. Install the [Claude GitHub App](https://github.com/apps/claude).
2. Add an org-level `CLAUDE_CODE_OAUTH_TOKEN` Actions secret scoped to the
   repos — the token `/install-github-app` provisions for a Claude Max/Pro
   subscription. `secrets: inherit` forwards it into the workflow. (Using the
   direct Claude API instead? Forward `ANTHROPIC_API_KEY` and swap the input —
   see the workflow header.)

Inputs — the complete `workflow_call` schema. Neither is required:

| Input            | Type     | Default   | Purpose                                                         |
|------------------|----------|-----------|-----------------------------------------------------------------|
| `trigger_phrase` | `string` | `@claude` | Phrase that summons Claude in an issue/PR/comment.              |
| `claude_args`    | `string` | _(empty)_ | Verbatim Claude Code CLI args, e.g. `--model … --max-turns 10`. |

There is intentionally **no automatic per-PR review**: the workflow exposes no
`prompt` input and wires no `pull_request` trigger, so `claude-code-action@v1`
stays in interactive mode. Automatic review would be a separate opt-in
workflow.

## release-tag

Mints the annotated `vX.Y.Z` tag on merge to `main` by reading the repo's
declared version, wrapping the `actions/release-tag` composite action.
Idempotent: if the tag already exists the run is a no-op, so bumping the
version in the release PR is the whole release action.

An anti-downgrade guard compares the declared version against the latest tag
reachable from `HEAD` and fails the run if it is not greater (disable with
`enforce-increase: false`). The tag is always **annotated** — `bundle-lib.sh`
and `git describe` rely on that.

Inputs — the complete `workflow_call` schema. None is required:

| Input              | Type      | Default          | Purpose                                                             |
|--------------------|-----------|------------------|---------------------------------------------------------------------|
| `version-file`     | `string`  | `pyproject.toml` | Path to the file holding the declared version.                      |
| `version-source`   | `string`  | `pyproject`      | How to read it: `pyproject` \| `plain` \| `package-json`.            |
| `tag-prefix`       | `string`  | `v`              | Tag name prefix.                                                    |
| `enforce-increase` | `boolean` | `true`           | Fail if the declared version is not greater than the latest tag.    |
| `dry-run`          | `boolean` | `false`          | Compute and log the tag but do not create it.                       |

Repos with no `pyproject.toml` point at their own version file instead — a
one-line `VERSION` file with `version-source: plain`, or a `package.json` with
`version-source: package-json`:

```yaml
    with:
      version-file: VERSION
      version-source: plain
```

The workflow is **ref-locked, not tag-pinned**: it resolves
`github.job_workflow_ref`, checks this repo out at that exact ref, and runs the
composite action from there — so workflow and action are always the same
revision, with no mutable tag in between.
