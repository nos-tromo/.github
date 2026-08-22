# Reusable workflows

Reference for the four reusable workflows this repo ships. The
[top-level README](../README.md) carries the minimal caller snippet for each
one; this file carries the input schemas, prerequisites and options around
them.

Two things apply to every caller:

- The doubled `.github/.github/` in a `uses:` path is correct — the repo is
  *named* `.github`.
- Every `uses:` ref must be pinned to a full 40-character commit SHA with the
  version in a trailing comment. See
  [pinning.md](pinning.md#action-refs) for why, how to resolve a tag's SHA,
  and the check that enforces it.

## python-app-ci

Lint (strict-config drift + pre-commit), a pytest matrix, and optional
`docker compose build` and React/pnpm frontend jobs, for the four Python apps.

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

A consumer with a `frontend/` must also pin `@infra/ui` correctly, or both the
`frontend` and `docker` jobs fail — see
[pinning.md](pinning.md#infra-ui-tarball-pins).

## infra-validation

yamllint, shellcheck, hadolint, and `docker compose config` validation for the
infra repos.

Inputs:

| Input                | Default              | Purpose                                                                            |
|----------------------|----------------------|------------------------------------------------------------------------------------|
| `compose-files`      | _(empty)_            | Space-separated `-f` arguments for `docker compose config`. Omit to skip the `compose-config` job (infra repos that own no compose, e.g. `deploy`). |
| `compose-profiles`   | _(empty)_            | Space-separated `--profile` arguments.                                             |
| `dockerfiles-glob`   | `docker/Dockerfile.*`| Glob for hadolint (fails only on `error`-level findings).                          |
| `shell-scripts-glob` | `scripts/*.sh`       | Glob for shellcheck.                                                               |

## node-lib-ci

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

Optional inputs:

| Input            | Default   | Purpose                                                         |
|------------------|-----------|-----------------------------------------------------------------|
| `trigger_phrase` | `@claude` | Phrase that summons Claude in an issue/PR/comment.              |
| `claude_args`    | _(empty)_ | Verbatim Claude Code CLI args, e.g. `--model … --max-turns 10`. |

There is intentionally **no automatic per-PR review**: the workflow exposes no
`prompt` input and wires no `pull_request` trigger, so `claude-code-action@v1`
stays in interactive mode. Automatic review would be a separate opt-in
workflow.

