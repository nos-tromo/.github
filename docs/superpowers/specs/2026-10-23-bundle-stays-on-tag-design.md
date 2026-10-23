# Bundle stays on the tag — `make bundle` leaves HEAD on the release it built

- **Date:** 2026-10-23
- **Status:** Approved design
- **Scope:** Federation-wide (every repo whose `scripts/bundle_images.sh` calls
  `bundle_checkout_release`)
- **Canonical config repo:** `nos-tromo/.github`
- **Supersedes:** the restore-on-every-exit behavior of `bundle_checkout_release`
  in `2026-07-03-bundle-release-checkout-design.md` (§3.1's EXIT trap and §7's
  "No stranded detached HEAD"). Tag resolution, the dirty-tree and no-tag refusals,
  and the override short-circuit are unchanged.

## 0. Sensitivity

Generic version-from-git build tooling, public-safe like its predecessor. Keep the
prose here and in downstream docs in the same neutral register.

## 1. Problem

`make bundle` builds the latest annotated tag reachable from HEAD, then switched back
to the operator's branch. When the branch is ahead of that tag, the repo is left on
different files from the ones the images were built from. Anything that ships the
images *together with* files from the repo (compose files, Makefile, mounted
config) then pairs images from the tag with files from the branch. A consumer that
checks this pairing reports a version skew it cannot clear by re-running
`make bundle`, because that rebuilds the same tag and switches back again.

## 2. Decision

`bundle_checkout_release` leaves HEAD on the tag after a **successful** bundle:

- **Success** — HEAD stays detached on the tag, and the lib prints how to return
  (`git switch <branch>`). The repo files now match the bundled images.
- **Failure** — the EXIT trap switches back to where the operator was, so a failed
  build never leaves a repo detached with no artifact to show for it.
- **Already at the tag's commit** — nothing is switched; the clean tree already is
  the tagged tree, so the build runs in place and the branch is kept. This is the
  common case right after a release lands.

Bundling the branch tip stays what `make bundle-dev` is for.

All movement uses `git switch` rather than `git checkout`: `switch` only moves HEAD
and never falls back to restoring a same-named path, and its required `--detach`
makes the detached state explicit. Returning to a detached starting commit needs
`--detach` while returning to a branch must not have it, so the lib records which
one it started from (`_BUNDLE_ORIG_DETACHED`).

## 3. Consequences

- Operators return with `git switch <branch>` (or whatever pull/refresh tooling they
  already run) once the bundle has been shipped.
- Consumers that skip rebuilding an already-bundled tag should switch to that tag
  on the skip path too, or the skip path reintroduces the mismatch.
- A repo whose `bundle_images.sh` never calls `bundle_checkout_release` is unaffected.

## 4. Files

- `configs/bundle/bundle-lib.sh` — `bundle_checkout_release` + the new
  `_bundle_checkout_exit` EXIT handler.
- `configs/make-common/common.mk` — `bundle` comment.
- `tests/bundle_checkout_smoke.sh` — case 1/5 now assert HEAD stays on the tag;
  new cases: failure returns to the branch (6), failure returns to a detached
  starting commit (7), no switch when already at the tag (8).
- `tests/fixtures/{bundle,mk}-aligned/` — mirror the canonical files.
- Consumers re-vendor `scripts/bundle-lib.sh` (and `make/common.mk` where included)
  together with their pin bump.
