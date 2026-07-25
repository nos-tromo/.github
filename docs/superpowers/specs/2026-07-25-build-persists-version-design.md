# `make build` persists the built tag (#36) — design

Status: approved design, pre-implementation
Date: 2026-07-25
Scope: canonical `configs/make-common/common.mk` (released as v3.9) + the
standard re-vendor wave to its five consumers (chorus, docint, Nextext,
translator, vllm-service).

## Problem (issue #36)

The dev image tag (`YYYY-MM-DD[-shortsha]`) is recomputed lazily on every
make invocation: `<REPO_UC>_VERSION ?= cat .<repo>-version || compute`.
The `.<repo>-version` file is written only by the bundle path
(`bundle_version` in `scripts/bundle-lib.sh`), so on dev hosts that use
`make build` it never exists and every target recomputes. After a date
rollover or any new commit, `make up` (deliberately `--no-build`)
references a tag that was never built: compose removes the running
container, then fails to create its replacement — the working image is
still on disk under the old tag, unnamed and unfound. This stranded the
app tier during deploy's edge-tier verification.

## Decision

**`make build` persists the tag it builds** to `.<repo>-version` — the
same file the bundle path already writes — making the file's meaning
uniform: *the image version currently deployable on this host*. Written
by `build` and by `bundle`/`bundle-dev`; read (via the existing `?=`
chain) by everything else.

Mechanics in the canonical `build:` recipe:

1. Compute a **fresh** `date+sha` at build time, ignoring any existing
   `.<repo>-version` (a stale file must not make `build` re-produce an
   old tag name for new content — tags stay honest: each build is
   stamped with its real date and commit).
2. Run the compose build with that version exported (compose interpolates
   it into the image tags).
3. On build success only, write it to `.<repo>-version`.

An explicit `<REPO_UC>_VERSION` exported by the caller keeps winning (it
already precedes the `?=`), and `build` then persists that override —
what was built is what the file names.

## What deliberately does not change

- `up`/`up-dev` stay `--no-build`, and a fresh clone that has never built
  still fails loudly on `up` (computed fallback, image absent) without
  touching any container — that behavior is correct today.
- The bundle path (`bundle_version`, `BUNDLE_DEV`, overrides) is
  untouched; it already persists the file.
- Rejected alternatives: falling back to the newest matching local image
  at `up` time (heuristic, masks forgot-to-rebuild, adds docker-query
  plumbing to every `up`); static dev tags (destroys build identity that
  staging soaks rely on).

## Rollout

1. Canonical change in `nos-tromo/.github` + header-comment update in the
   VERSION block; PR, CI green, merge, release **v3.9** (this repo's
   established tag cadence).
2. Re-vendor wave: one PR per consumer (chorus, docint, Nextext,
   translator, vllm-service) copying the canonical file byte-identical to
   `make/common.mk` (the drift check enforces this). Each repo's CI
   gates its own PR. Confirm `.<repo>-version` is gitignored in each
   consumer (the bundle path has been writing it all along, so it should
   be already — fix any gap in the same PR).

## Verification

In translator on the dev host (vendored copy updated):
1. `make build` → `.translator-version` exists and equals the tag of the
   image just built (`docker images` shows it).
2. Regression reproduction: overwrite `.translator-version` with a bogus
   `1999-01-01-deadbee`, run `make build` → file corrected to the fresh
   real tag (stale file did not leak into the build).
3. The original failure sequence, now healed: `make build`, then simulate
   a day/commit rollover by deleting nothing and just re-running
   `make up` after the file exists — `up` must use the file's tag (verify
   via `docker compose config | grep image:`), not a recomputed one; the
   container recreates/no-ops against the existing image.
4. Fresh-clone shape: temporarily move `.translator-version` away and
   confirm `make -n up`'s interpolated tag falls back to computed (loud
   `--no-build` failure preserved), then restore.
