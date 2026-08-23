# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`nos-tromo/.github` — the **public** org-wide CI + shared-build-glue repo for the
`nos-tromo` federation (~12 repos; see `profile/README.md` for the map). It ships
no application code. It provides two things to consumer repos:

1. **Reusable GitHub Actions workflows** (`.github/workflows/*.yml`, `on: workflow_call`).
2. **Canonical shared config/library files** (`configs/`) that consumers mirror,
   drift-checked in CI by `scripts/validate_*.py`.

`README.md` is the consumer-facing entry point: the inventory plus one copyable caller
snippet per workflow. `docs/` holds the reference behind it — input schemas
(`docs/workflows.md`), the two commit-SHA pinning policies (`docs/pinning.md`), the
strict-Python and vendored-file contracts, versioning. This file is for working
*inside* this repo — read those for consumer-side detail rather than duplicating it here.

## The two core patterns

**Reusable workflows.** Consumers call these as
`uses: nos-tromo/.github/.github/workflows/<name>.yml@<40-hex-sha>  # v3.14` — the
doubled `.github/.github/` is correct (the repo is *named* `.github`), and the ref is
a full commit SHA, never a tag, per this repo's own pin policy (`docs/pinning.md`).
All five:
- `python-app-ci.yml` — lint (all six validators + pre-commit) → pytest matrix → optional
  frontend and docker jobs. `run-tests: false` gives a lint-only run (vllm-service), and
  skips the docker job with it (`docker` declares `needs: test`).
- `infra-validation.yml` — yamllint/shellcheck/hadolint/`docker compose config` for infra
  repos, plus a `make-common` job running the `common.mk` + `bundle-lib.sh` + action-pin checks.
- `node-lib-ci.yml` — pnpm lint/typecheck/test/build for `@infra/ui`, with optional
  `check-dist`, plus a dedicated `action-pins` job.
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
- **Two-step release.** The hub is hand-tagged (it wires no `release-tag.yml` caller of its
  own; git tags are the only version record — there is no `VERSION` file). Cutting a version is
  (1) tag the merge commit with the next immutable minor (`git tag -a v3.15 -m … &&
  git push origin v3.15`), then (2) force-move the major alias (`git tag -f -a v3 -m … &&
  git push origin v3 --force`). Forgetting step 2 silently strands `@v3` consumers on the old
  commit. Check the current latest with `git tag --sort=-v:refname | head -1`.
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

# Bash behavior smoke tests (all three run in self-ci):
bash tests/bundle_version_smoke.sh     # bundle-lib-smoke
bash tests/bundle_checkout_smoke.sh    # bundle-lib-smoke
bash tests/build_persist_smoke.sh      # make-common-smoke

# Unit tests for the release-tag action (pytest; run in self-ci's release-tag-unit job):
cd actions/release-tag && uv run --with pytest python -m pytest -q
# single test:
cd actions/release-tag && uv run --with pytest python -m pytest test_extract_version.py::test_extract_pyproject -q
```

`self-ci.yml` runs on every PR/push here and is the source of truth for what "green" means:
in eight jobs it lints `scripts/`, runs each validator against an aligned fixture (must pass), a
drifted fixture (must fail) and the opt-in edge cases, and pytests the release-tag action. When
you add or change a validator, add its smoke job there too. Full job map: `docs/maintaining.md`.

## Layout

- `.github/workflows/` — reusable workflows (above) + `self-ci.yml` (this repo's own CI).
- `actions/release-tag/` — composite action; `extract_version.py` + its pytest suite.
- `configs/` — canonical shared files: `python-strict/`, `make-common/`, `bundle/`, `frontend-eslint/`.
- `scripts/` — the stdlib-only drift validators plus the action-pin policy check.
- `tests/fixtures/` — per-validator `*-aligned` / `*-drifted` / `*-absent` / `*-required-absent` fixtures
  (the strict-config set is unprefixed: `aligned` / `drifted` / `half-migrated`; the `pins-*` set uses
  invented placeholder SHAs); `tests/*.sh` are bash smoke tests.
- `docs/` — the consumer-facing reference set (`workflows.md`, `pinning.md`, `strict-python.md`, `vendored-files.md`, `versioning.md`, `maintaining.md`), indexed by `docs/README.md`.
- `docs/superpowers/specs/` and `docs/superpowers/plans/` — dated design specs and implementation plans (this repo uses the brainstorm → spec → plan workflow; read the relevant spec before changing bundle/release behavior).
