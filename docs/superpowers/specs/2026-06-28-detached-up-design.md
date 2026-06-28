# Uniform detached, no-build `make up` across the federation

- **Date:** 2026-06-28
- **Status:** Approved (brainstorming); implementation in progress
- **Repos touched:** `.github` (canonical), `chorus`, `docint`, `Nextext`, `translator`, `vllm-service`, `deploy`
- **Unchanged:** `data-plane`, `open-webui-service` (already `-d --no-build`)

## Problem

`make up` was inconsistent across members. `up`/`up-dev` live in the canonical
`configs/make-common/common.mk` as a parametrized recipe
(`$(UP_ENV) $(COMPOSE) up $(UP_FLAGS)`, `UP_FLAGS` default empty → **foreground**),
and each consumer set `UP_FLAGS` differently:

| Repo | `UP_FLAGS` | `make up` before |
|---|---|---|
| chorus | `-d` | detached, builds |
| docint | — | foreground, builds |
| Nextext / translator / vllm-service | `--no-build` | foreground, no-build |
| data-plane, open-webui *(bespoke)* | n/a | detached, `-d --no-build` |

Because several `make up` targets ran in the foreground, the `deploy` federation
layer could not chain them and **bypassed each repo's `make up`** with direct
`docker compose up -d --no-build` calls — a documented workaround whose exit
condition was "if the members later grow a detached production `up`, switch back
to delegating."

## Decision

**Full uniform production-shape `up`.** `up` and `up-dev` become `-d --no-build`
everywhere — detached, never building — matching the bespoke pulled-image members
exactly. A `make dev` convenience (`build` + `up-dev`) absorbs the build-first
step for the locally-built apps. `deploy` then delegates `make up` instead of
bypassing it.

## Changes

### 1. Canonical `common.mk`

```make
up:
	$(COMPOSE) up -d --no-build
up-dev:
	$(COMPOSE_DEV) up -d --no-build
dev: build up-dev
```

- `-d --no-build` baked into `up`/`up-dev` (was `$(UP_ENV) … up $(UP_FLAGS)`).
- **`UP_FLAGS` and `UP_ENV` removed** — vestigial once `-d --no-build` is fixed
  (`UP_ENV` only carried `DOCKER_BUILDKIT=1` for building, which no longer happens
  on `up`; `BUILD_ENV` still applies to the `build` target).
- New `dev: build up-dev` target; added to `.PHONY`.
- Header comment updated to document the detached/no-build semantics.
- Self-CI fixtures re-vendored: `tests/fixtures/mk-aligned/make/common.mk`
  (byte-identical to canonical) and `tests/fixtures/mk-drifted/make/common.mk`
  (canonical + the existing single-line `pytest -q`→`pytest` drift).

### 2. Consumers (×5: chorus, docint, Nextext, translator, vllm-service)

Per repo: re-vendor `make/common.mk` verbatim from the new canonical; **delete the
now-dead `UP_FLAGS` / `UP_ENV` lines** from the pre-include block; update the
per-repo `help` text and CLAUDE.md/README "Commands" wording (`up` is
detached/no-build; build first, or `make dev`).

### 3. `deploy`

`up` delegates instead of bypassing:

```make
$(MAKE) -C $(INFRA_ROOT)/$(VLLM_DIR) up           # then wait-healthy
$(MAKE) -C $(INFRA_ROOT)/$(DATA_DIR) up PROFILE=$(DATA_PROFILE)   # then wait-healthy
@for a in $(APP_DIRS) $(OPENWEBUI_DIR); do $(MAKE) -C $(INFRA_ROOT)/$$a up; done
```

Tier ordering and `wait-healthy.sh` gates unchanged. `ps`/`logs` stay
compose-direct (no uniform `ps`; `make logs` follows with `-f`), so the `compose`
helper remains. The stale top NOTE and the "central design split" section are
rewritten — `up` now joins the delegated targets.

## Rollout

1. **`.github` PR** (canonical `common.mk` + fixtures + this spec). Merge →
   cut immutable **`v3.2`** and **move the `v3` alias** to it.
2. **Re-vendor the 5 consumers** promptly. The moment `v3` moves, each consumer's
   `make-common` check compares its *old* vendored copy against the *new* canonical
   → red until re-vendored. This transient is the known cost of the moving-alias
   model; minimized by having all 5 consumer PRs ready to merge right after the tag
   moves. (Consumer PR branches only go green *after* `v3` moves.)
3. **`deploy` PR** is independent of the tag — lands anytime after the consumers'
   `up` is detached.

## Dev-workflow change (accepted)

`make up` / `make up-dev` no longer build. Dev brings a built app up with
`make build && make up-dev`, or `make dev`. Production loads/pulls images
(`make load` / `pull`) before `make up`, as it already did.

## Verification

- `make -n up` / `up-dev` / `dev` show `-d --no-build` (and `dev` = build→up-dev).
- `python scripts/validate_make_common.py --consumer-root .` green in each consumer
  after re-vendor; `.github` self-CI `make-common-smoke` green (fixtures).
- `make -n up` in `deploy` shows delegation (`$(MAKE) -C … up`).
- Existing per-repo lint/parse CI.

## Out of scope

- `common.mk` `up` still does not depend on `network`/`volumes` (pre-existing;
  bespoke repos differ — a separate concern).
- The build-vs-pull difference between locally-built apps and pulled members.
