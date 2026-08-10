# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`nos-tromo/.github` — the **public** org-wide CI + shared-build-glue repo for the
`nos-tromo` federation (~11 repos; see `profile/README.md` for the map). It ships
no application code. It provides two things to consumer repos:

1. **Reusable GitHub Actions workflows** (`.github/workflows/*.yml`, `on: workflow_call`).
2. **Canonical shared config/library files** (`configs/`) that consumers mirror,
   drift-checked in CI by `scripts/validate_*.py`.

`README.md` is the consumer-facing manual (input schemas, usage snippets, `@infra/ui`
pinning, Claude-mention setup). This file is for working *inside* this repo — read
the README for consumer-side detail rather than duplicating it here.

## The two core patterns

**Reusable workflows.** Consumers call these as
`uses: nos-tromo/.github/.github/workflows/<name>.yml@v2`. The doubled `.github/.github/`
is correct — the repo is *named* `.github`. The main ones:
- `python-app-ci.yml` — lint (strict-config drift + pre-commit) → pytest matrix → optional docker/frontend jobs.
- `infra-validation.yml` — yamllint/shellcheck/hadolint/`docker compose config` for infra repos.
- `node-lib-ci.yml` — pnpm lint/typecheck/test/build for `@infra/ui`, with optional `check-dist`.
- `claude.yml` — **manual `@claude` only, no automatic per-PR review** (deliberate: exposes no `prompt` input, wires no `pull_request` trigger).
- `release-tag.yml` — mints an annotated `vX.Y.Z` tag on merge, wrapping `actions/release-tag`.
  Its self-reference is **ref-locked, not tag-pinned**: it resolves `github.job_workflow_ref`,
  checks this repo out at that exact ref, and runs `./.nos-tromo-github-ref/actions/release-tag`.
  Workflow and composite action are therefore always the same revision, with no mutable tag between.

**Canonical config + drift-check.** Canonical files live in `configs/`; each has a
validator in `scripts/` that fails CI on drift. Two comparison flavors:
- **Semantic merge** (`python-strict`): `configs/python-strict/{ruff.toml,pyrefly.toml,precommit-versions.yaml}`
  is merged into each consumer's `pyproject.toml` / `.pre-commit-config.yaml` and compared
  key-by-key. `validate_strict_config.py`. Only `[tool.ruff] target-version` may be overridden.
- **Verbatim vendor** (`make/common.mk`, `bundle-lib.sh`, `eslint.config.js`): copied
  byte-for-byte into consumers and compared with an exact file diff. **Never hand-edit the
  vendored copy — change the canonical file and re-vendor.**

`validate_action_pins.py` is a fifth validator that fits neither flavor: it has no canonical
file at all. It is a **policy check on the consumer's own workflows** — every `uses:` ref must
name a full 40-hex commit SHA (local `./path` actions and `docker://…@sha256:` digests exempt;
no `.github/workflows/` at all = skip). It runs in `python-app-ci`'s lint job, `infra-validation`'s
make-common job, and `node-lib-ci`'s `action-pins` job, and self-ci runs it against this repo.

`validate_infra_ui_pin.py` is a sixth validator of the same policy flavor, closing the same
seam one layer up: the app frontends' `@infra/ui` dependency must be a
`https://codeload.github.com/nos-tromo/infra-ui/tar.gz/<40-hex>` URL, never a mutable tag ref
(and never the `github:` shorthand Dependabot rewrites to git+SSH). It checks the manifest and
`pnpm-lock.yaml` both — pnpm stores no integrity hash for tarball URLs, so a stale tag-form
lockfile would keep resolving the tag even after the manifest was fixed. Skips repos without a
frontend or without the dep. Runs in `python-app-ci`'s lint job beside the action-pin check.

## Invariants you must preserve

These are the non-obvious rules that keep the system coherent:

- **Ref-locked validation.** `python-app-ci.yml`'s lint job checks out *this repo at the
  same ref the workflow is running at* (`github.job_workflow_ref`, not `github.workflow_ref`)
  and validates the consumer against it. So a consumer pinned to `@vN` is validated against
  the canonical config that shipped with `vN`. **Consequence:** a canonical-config change and
  the consumers' mirrored updates must land/tag *together*, or consumers' lint jobs break.
- **Fixtures mirror canonical.** When you change anything under `configs/python-strict/`,
  update `tests/fixtures/aligned/` to match — it's the same drift signal real consumers get,
  applied to this repo's own smoke test. Likewise `tests/fixtures/{mk,bundle,eslint}-aligned/`
  must mirror their canonical source.
- **Include-driven required-ness.** A vendored file is enforced only where the consumer opts
  in: `make/common.mk` iff the `Makefile` has `include make/common.mk`; `bundle-lib.sh` iff
  `scripts/bundle_images.sh` sources it; `eslint.config.js` only when present. vendored-and-opted-in
  → drift-checked; missing-but-opted-in → **fails**; missing-and-not-opted-in → skipped. This is
  self-maintaining (no exemption list); don't reintroduce one.
- **Validators are stdlib-only.** `scripts/*.py` hand-roll their YAML/pre-commit parsing rather
  than importing PyYAML, so they run in any consumer's environment with no install. Keep it that
  way. They require Python 3.11+ (`tomllib`).
- **Two-step release.** Cutting a version is (1) tag the merge commit with the next immutable
  minor (`git tag -a v2.10 -m … && git push origin v2.10`), then (2) force-move the major alias
  (`git tag -f -a v2 -m … && git push origin v2 --force`). Forgetting step 2 silently strands
  `@v2` consumers on the old commit.
- **Annotated tags are load-bearing.** `bundle-lib.sh` and `actions/release-tag` rely on
  `git describe` seeing *annotated* tags only (no `--tags`), so a stray lightweight tag can never
  be mistaken for a release. Always tag with `-a`.
- **Public repo → neutral register.** Keep all prose (code comments, docs, this file) free of
  host topology, airgap/hand-carry mechanics, machine roles, or deployment specifics. Design docs
  carry an explicit sensitivity note for this reason.

## Common commands

Everything runs from the repo root. The only tooling needed is Python 3.11+ and `uv`/`uvx`.

```bash
# Run a drift validator against a fixture or a real consumer (exit 0 = aligned, 1 = drift):
python3 scripts/validate_strict_config.py --consumer-root tests/fixtures/aligned
python3 scripts/validate_strict_config.py --consumer-root ../chorus   # real consumer
python3 scripts/validate_make_common.py   --consumer-root tests/fixtures/mk-aligned
python3 scripts/validate_bundle_lib.py    --consumer-root tests/fixtures/bundle-aligned
python3 scripts/validate_eslint_config.py --consumer-root tests/fixtures/eslint-aligned
python3 scripts/validate_action_pins.py   --consumer-root tests/fixtures/pins-aligned
python3 scripts/validate_action_pins.py   --consumer-root .   # the hub is subject to its own pin policy
python3 scripts/validate_infra_ui_pin.py  --consumer-root tests/fixtures/uipin-aligned

# Lint scripts/ exactly as self-ci does — pinned ruff version, canonical config:
VER=$(grep '^ruff:' configs/python-strict/precommit-versions.yaml | awk '{print $2}' | tr -d '"' | sed 's/^v//')
uvx "ruff@$VER" check  --config configs/python-strict/ruff.toml scripts/
uvx "ruff@$VER" format --config configs/python-strict/ruff.toml --check scripts/

# Bash smoke tests for the bundle library:
bash tests/bundle_version_smoke.sh
bash tests/bundle_checkout_smoke.sh

# Unit tests for the release-tag action (pytest; NOT wired into self-ci — run manually):
cd actions/release-tag && uv run --with pytest python -m pytest -q
# single test:
cd actions/release-tag && uv run --with pytest python -m pytest test_extract_version.py::test_extract_pyproject -q
```

`self-ci.yml` runs on every PR/push here and is the source of truth for what "green" means:
it lints `scripts/`, then runs each validator against an aligned fixture (must pass), a drifted
fixture (must fail), and the opt-in edge cases. When you add or change a validator, add its
smoke job there too.

## Layout

- `.github/workflows/` — reusable workflows (above) + `self-ci.yml` (this repo's own CI).
- `actions/release-tag/` — composite action; `extract_version.py` + its pytest suite.
- `configs/` — canonical shared files: `python-strict/`, `make-common/`, `bundle/`, `frontend-eslint/`.
- `scripts/` — the stdlib-only drift validators plus the action-pin policy check.
- `tests/fixtures/` — per-validator `*-aligned` / `*-drifted` / `*-absent` / `*-required-absent` fixtures
  (the `pins-*` set uses invented placeholder SHAs); `tests/*.sh` are bash smoke tests.
- `docs/superpowers/specs/` and `docs/superpowers/plans/` — dated design specs and implementation plans (this repo uses the brainstorm → spec → plan workflow; read the relevant spec before changing bundle/release behavior).
