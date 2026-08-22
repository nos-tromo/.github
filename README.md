# nos-tromo/.github

Org-wide CI assets for the [nos-tromo](https://github.com/nos-tromo)
federation: five reusable GitHub Actions workflows, the canonical strict-mode
Python lint/type config, and the shared build-glue files consumers vendor
verbatim — all drift-checked in CI. It ships no application code.

This page is the inventory plus one copyable caller snippet per workflow; the
reference behind it lives in [docs/](docs/README.md).

## What's here

| Path | What it provides |
|---|---|
| [`.github/workflows/python-app-ci.yml`](.github/workflows/python-app-ci.yml) | CI for the four Python apps ([chorus](https://github.com/nos-tromo/chorus), [docint](https://github.com/nos-tromo/docint), [Nextext](https://github.com/nos-tromo/Nextext), [translator](https://github.com/nos-tromo/translator)): pre-commit (ruff + pyrefly via the consumer's own `.pre-commit-config.yaml`), pytest across a Python-version matrix, and optional `docker compose build` and React/pnpm frontend jobs. |
| [`.github/workflows/infra-validation.yml`](.github/workflows/infra-validation.yml) | CI for the infra repos ([vllm-service](https://github.com/nos-tromo/vllm-service), [data-plane](https://github.com/nos-tromo/data-plane), [deploy](https://github.com/nos-tromo/deploy)): yamllint, shellcheck, hadolint, and `docker compose config` validation. |
| [`.github/workflows/node-lib-ci.yml`](.github/workflows/node-lib-ci.yml) | CI for the shared Node/TypeScript library ([infra-ui](https://github.com/nos-tromo/infra-ui), the `@infra/ui` design system): pnpm lint, typecheck, test, build, and an optional check that a committed prebuilt `dist/` is in sync with source. |
| [`.github/workflows/release-tag.yml`](.github/workflows/release-tag.yml) | Mints the annotated `vX.Y.Z` tag on merge to `main` from a repo's declared version. Idempotent, with an anti-downgrade guard. |
| [`.github/workflows/claude.yml`](.github/workflows/claude.yml) | **Manual** `@claude` invocation (interactive/tag mode) in a consumer repo. Acts only when a human mentions `@claude`; deliberately has **no** automatic per-PR review. |
| [`configs/`](configs/) | Canonical shared files consumers mirror: [`python-strict/`](configs/python-strict/) (ruff, pyrefly, pre-commit versions), [`make-common/`](configs/make-common/), [`bundle/`](configs/bundle/), [`frontend-eslint/`](configs/frontend-eslint/). |
| [`scripts/`](scripts/) | The stdlib-only drift validators the workflows invoke, plus the action-pin and `@infra/ui` pin policy checks. |
| [`.github/dependabot.yml`](.github/dependabot.yml) | Org-default dependabot template (also runs on this repo for `github-actions` updates, one entry per directory that holds workflows or actions). |

## Calling a workflow

Each snippet is a complete caller file. The doubled `.github/.github/` is
correct — the repo is *named* `.github` — and every `uses:` ref must be pinned to
a **full 40-character commit SHA** with the version in a trailing comment
([docs/pinning.md](docs/pinning.md#action-refs)). Full input schemas for all
five: [docs/workflows.md](docs/workflows.md).

### Python apps

```yaml
# chorus/.github/workflows/ci.yml
name: ci
on:
  pull_request:
  push:
    branches: [main]

jobs:
  ci:
    uses: nos-tromo/.github/.github/workflows/python-app-ci.yml@<commit-sha>  # v3.14
    with:
      python-versions: '["3.12", "3.13"]'
```

Options (`uv-sync-args`, `docker-build`, `frontend-build`, `test-env`, …) in
[workflows.md](docs/workflows.md#python-app-ci). A consumer with a frontend must
also pin `@infra/ui` as a commit-SHA tarball URL:
[pinning.md](docs/pinning.md#infra-ui-tarball-pins).

### Infra repos

```yaml
# vllm-service/.github/workflows/ci.yml
name: ci
on:
  pull_request:
  push:
    branches: [main]

jobs:
  ci:
    uses: nos-tromo/.github/.github/workflows/infra-validation.yml@<commit-sha>  # v3.14
    with:
      compose-files: "-f docker/compose.yaml -f docker/compose.override.yaml"
      compose-profiles: "--profile cpu --profile cuda"
```

Omit `compose-files` to skip the `compose-config` job, as `deploy` does; hadolint
and shellcheck globs in [workflows.md](docs/workflows.md#infra-validation).

### Node library

```yaml
# infra-ui/.github/workflows/ci.yml
name: CI
on:
  pull_request:
  push:
    branches: [main]

jobs:
  ci:
    uses: nos-tromo/.github/.github/workflows/node-lib-ci.yml@<commit-sha>  # v3.14
    with:
      check-dist: true
```

Per-step toggles and `dist-dir` in [workflows.md](docs/workflows.md#node-lib-ci).

### Release tagging

The caller must grant `contents: write` — a reusable workflow's token
permissions come from the caller.

```yaml
# <consumer>/.github/workflows/release-tag.yml
name: release-tag
on:
  push:
    branches: [main]

permissions:
  contents: write

concurrency:
  group: release-tag-${{ github.ref }}

jobs:
  tag:
    uses: nos-tromo/.github/.github/workflows/release-tag.yml@<commit-sha>  # v3.14
    with:
      version-file: pyproject.toml
```

Repos with no `pyproject.toml` point at a `VERSION` file or `package.json`
instead — see [workflows.md](docs/workflows.md#release-tag).

### Claude mentions

The comment/issue triggers must live in the caller (a reusable workflow can't
declare triggers that fire in another repo):

```yaml
# <consumer>/.github/workflows/claude.yml
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
    uses: nos-tromo/.github/.github/workflows/claude.yml@<commit-sha>  # v3.14
    secrets: inherit
```

Two one-time org-wide prerequisites (the Claude GitHub App and a
`CLAUDE_CODE_OAUTH_TOKEN` secret) must be in place before it can act; those and
the optional `trigger_phrase` / `claude_args` inputs are in
[workflows.md](docs/workflows.md#claude).

## Working in this repo

Self-CI ([`.github/workflows/self-ci.yml`](.github/workflows/self-ci.yml)) lints
`scripts/` with the canonical strict config and smoke-tests every validator
against aligned and drifted fixtures on each PR. To run one against a real
consumer (Python 3.11+, uses `tomllib`):

```bash
python3 scripts/validate_strict_config.py --consumer-root ../chorus
```

[docs/maintaining.md](docs/maintaining.md) has the full self-CI contract;
[CLAUDE.md](CLAUDE.md) the invariants to preserve when changing anything here.

## Documentation

[docs/README.md](docs/README.md) indexes the reference set: per-workflow inputs,
the commit-SHA pinning policies, the canonical Python config consumers mirror,
the vendored build glue, the tag ritual, and maintaining this repo. The
federation map is in [`profile/README.md`](profile/README.md).
