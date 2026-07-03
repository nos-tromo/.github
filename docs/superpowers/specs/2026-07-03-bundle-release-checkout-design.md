# Bundle release checkout — `make bundle` builds the latest tag, `make bundle-dev` builds local state

- **Date:** 2026-07-03
- **Status:** Approved design (pre-implementation)
- **Scope:** Federation-wide (the `nos-tromo` image-bundling repos on the shared `common.mk` / `bundle-lib.sh`)
- **Canonical config repo:** `nos-tromo/.github`
- **Supersedes:** §4.3–§4.4 of `infra/docs/2026-07-02-federation-release-workflow-design.md`
  (the on-tag-only / date+sha-fallback release mechanic). The GitHub-Flow branching
  model and the Gate-2 soak ritual from that doc are unchanged; only how `make bundle`
  resolves and stamps the version changes.

## 0. Sensitivity

This change is **generic version-from-git build tooling** — it reveals nothing about
host topology, airgap/hand-carry mechanics, machine roles, or the problem domain. Per
§0 of the federation release doc it is **public-safe** and ships in public
`nos-tromo/.github` as normal shared CI config. Keep all prose in this doc and the
downstream public docs (`deploy/README.md`, per-repo `CLAUDE.md`) in that neutral
register.

## 1. Problem

The shipped release mechanic (`.github` #27) conflates two things `make bundle` must
keep separate:

1. **`bundle` builds whatever is checked out.** `docker compose build` reads the
   working tree, not a git ref. The version *label* is computed from git (exact
   annotated tag at HEAD, else `date+sha`), but the *bits* are always the working
   tree. So a production bundle requires the operator to have manually checked out the
   exact tagged commit first — the tool does not guarantee it.
2. **The `date+sha` fallback is silently shippable.** If HEAD is not on a tag,
   `make bundle` still succeeds and emits `YYYY-MM-DD-<sha>`. That artifact is
   indistinguishable at the `make` level from a real release, yet carries no durable
   release identity. Bundling it for production is exactly the thing we do not want.

The desired split is by **intent**, expressed at the command level:

- **`make bundle`** → the **production** artifact: the latest release, versioned by an
  annotated tag, built from the tagged tree — regardless of where HEAD currently sits.
- **`make bundle-dev`** → the **current local state**: build the working tree as-is,
  stamped `date+sha` (or an explicit override), for dev iteration and pre-tag soak.

## 2. Goals / non-goals

**Goals**
- `make bundle` produces a tag-versioned artifact built from the tagged tree, or fails
  loudly. It never emits a `date+sha` artifact.
- `make bundle` works from anywhere on the release's line of history (e.g. `main` with
  HEAD advanced past the tag) — it resolves and checks out the tag itself, then
  restores the operator's branch.
- `make bundle-dev` preserves today's behavior (build the working tree, `date+sha` /
  override stamp) under an explicit, honestly-named target.
- The heavy logic lives once in the canonical, drift-checked `bundle-lib.sh`; the
  per-repo orchestrator change is a single line.

**Non-goals (YAGNI)**
- Auto-stashing a dirty tree (footgun; we abort instead).
- Auto-pushing tags, changelog generation, or signing.
- Changing `bundle_version()`'s precedence or the `common.mk` `build`/`up` version
  fallback (line 40) — both are untouched.
- `open-webui-service` (bespoke Makefile, not on `common.mk`) — out of scope, as in
  the federation doc.

## 3. Chosen approach

Add one function to canonical `bundle-lib.sh`, one target to canonical `common.mk`,
and a one-line mode guard to each consumer's `scripts/bundle_images.sh`.
`bundle_version()` is **unchanged**.

### 3.1 `bundle_checkout_release()` — new, in `configs/bundle/bundle-lib.sh`

`make bundle` calls this *before* the compose build. It resolves the latest annotated
tag reachable from HEAD, checks it out (detached) so the build below is the tagged
tree, and registers an `EXIT` trap that restores the operator's original branch — on
success, on build failure, or on interrupt. It refuses rather than ever producing an
unversioned production artifact.

```bash
# bundle_checkout_release <repo-slug>
#     PRODUCTION bundle only (`make bundle`). Resolve the latest ANNOTATED tag
#     reachable from HEAD, check it out (detached) so the compose build below is
#     the tagged tree, and register an EXIT trap restoring the original branch.
#     Refuses (nonzero) rather than ever bundling an unversioned artifact:
#       - dirty *tracked* working tree (a checkout would clobber it), or
#       - no annotated tag reachable from HEAD.
#     An explicit <REPO_UC>_VERSION_OVERRIDE short-circuits it entirely (bundle
#     the working tree as-is) - the dev escape hatch shared with `make bundle-dev`.
#     Untracked / gitignored files (e.g. .env) are ignored by the dirty check and
#     preserved across the checkout.
#     Call from scripts/bundle_images.sh BEFORE the compose build, dev-guarded:
#       [[ -n "${BUNDLE_DEV:-}" ]] || bundle_checkout_release chorus
bundle_checkout_release() {
  local repo="$1" repo_uc override_var override tag orig_ref
  repo_uc=$(printf '%s' "$repo" | tr 'a-z-' 'A-Z_')
  override_var="${repo_uc}_VERSION_OVERRIDE"
  override="${!override_var:-}"

  # Dev escape hatch: an explicit override bundles the working tree as-is.
  [[ -n "$override" ]] && return 0

  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    printf 'bundle: not a git repository - cannot resolve a release tag.\n' >&2
    printf "        Use 'make bundle-dev' to bundle the current local state.\n" >&2
    return 1
  fi

  # Refuse a dirty *tracked* tree; untracked/gitignored files (.env) are fine.
  if ! git diff --quiet || ! git diff --cached --quiet; then
    printf 'bundle: uncommitted changes to tracked files - refusing to check out a tag.\n' >&2
    printf "        Commit or stash them, or use 'make bundle-dev'.\n" >&2
    return 1
  fi

  # Latest ANNOTATED tag reachable from HEAD (no --tags => annotated-only,
  # matching bundle_version's --exact-match convention: a lightweight tag can
  # never become a release version).
  tag=$(git describe --abbrev=0 HEAD 2>/dev/null || true)
  if [[ -z "$tag" ]]; then
    printf 'bundle: no annotated release tag reachable from HEAD.\n' >&2
    printf "        Tag a release ('git tag -a vX.Y.Z -m ...') or use 'make bundle-dev'.\n" >&2
    return 1
  fi

  # Where to return: branch name if on one, else the detached SHA.
  orig_ref=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse HEAD)
  # Restore on ANY exit of the sourcing script (build success or failure).
  # Deferred expansion of the global avoids re-quoting the ref into the trap.
  _BUNDLE_ORIG_REF="$orig_ref"
  trap 'git checkout --quiet "$_BUNDLE_ORIG_REF"' EXIT

  printf 'bundle: building release tag %s (restoring %s afterwards)\n' "$tag" "$orig_ref"
  git checkout --quiet "$tag"
}
```

**Why these choices**

- **`git describe --abbrev=0 HEAD`** — the latest annotated tag *reachable from HEAD*.
  On `main` this is exactly "the latest release"; on any branch it can only ever be a
  tag the current history descends from (a clean-checkout ancestor). Annotated-only
  (no `--tags`) mirrors the existing `bundle_version` convention. Zero reachable tags →
  empty → hard error.
- **Dirty check via `git diff --quiet` + `git diff --cached --quiet`** — catches
  staged/unstaged changes to **tracked** files (which a checkout would clobber) while
  ignoring untracked and gitignored files. That is deliberate: `.env` and build
  outputs must survive the checkout, and `git checkout <tag>` leaves them in place.
- **Restore via `EXIT` trap on a global** — `bundle_images.sh` sources the lib into
  its own shell, so the trap fires when the orchestrator exits, whichever way. Using a
  global `_BUNDLE_ORIG_REF` with a single-quoted (deferred) trap body sidesteps
  re-quoting a ref that could contain shell metacharacters. Hard kills (SIGKILL) can't
  be trapped — the operator would `git checkout <branch>` by hand, same as any
  interrupted git operation.
- **Override short-circuit** — a set `<REPO_UC>_VERSION_OVERRIDE` skips resolve /
  guard / checkout entirely and bundles the working tree, matching the precedence
  `bundle_version()` already honors. It is the "I know what I'm doing" dev path even
  through `make bundle`.

`bundle_version()` needs **no change**: after `bundle_checkout_release` runs, HEAD is
exactly on the tag, so `bundle_version`'s existing `git describe --exact-match` branch
returns the tag verbatim and writes it to `.<repo>-version` as today.

### 3.2 `common.mk` — add the `bundle-dev` target

`bundle` stays the default (production) recipe; `bundle-dev` sets the mode flag. The
pair mirrors the existing `up` / `up-dev` convention.

```make
.PHONY: network volumes build bundle bundle-dev up up-dev dev stop down logs pre-commit

# Airgap release artifact. `bundle` is PRODUCTION: it builds the latest annotated
# tag reachable from HEAD (checks it out, builds, restores your branch) and
# refuses on a dirty tree or when no tag is reachable. `bundle-dev` bundles the
# current working tree as-is (date+sha / override) for dev iteration and soak.
bundle:
	./scripts/bundle_images.sh

bundle-dev:
	BUNDLE_DEV=1 ./scripts/bundle_images.sh
```

The `common.mk` header comment block (the `up`/`up-dev` note) gains a matching
`bundle`/`bundle-dev` line. No other `common.mk` change — the version-var fallback
(line 40) that feeds `build`/`up` is untouched.

### 3.3 Each consumer's `scripts/bundle_images.sh` — one-line guard

The per-repo orchestrator (bespoke: chorus/docint/Nextext/translator/vllm-service
build+save, data-plane pulls) gains exactly one line before the compose build:

```bash
. scripts/bundle-lib.sh
COMPOSE=(docker compose --env-file .env -f docker/compose.yaml)
[[ -n "${BUNDLE_DEV:-}" ]] || bundle_checkout_release chorus   # <-- added
bundle_version chorus; VER="$BUNDLE_VERSION"
"${COMPOSE[@]}" build
# ... partition / save unchanged ...
```

For the pull-only repo (`data-plane`) the same guard applies: checking out the tag
yields the tagged `compose.yaml` / `.env`, which is what pins the pulled image
versions that ship — so the tarball's provenance is the tag, uniformly.

**Constraint:** an orchestrator must not register its own conflicting `EXIT` trap
after sourcing the lib (none do today). If one later needs cleanup, it appends to the
restore, not replaces it.

## 4. The release ritual after this change

Gate 1 (pre-merge CI) and Gate 2 (staging soak of the *same* tarball) are unchanged.
Only the mechanics of producing the tarball simplify:

1. `main` is green and carries the features to ship.
2. (optional) iterate / soak arbitrary `main` state with `make bundle-dev`.
3. Tag it: `git tag -a v1.4.0 -m "…"` and push the tag.
4. `make bundle` **from anywhere on that history** → resolves `v1.4.0`, checks it out,
   builds, restores your branch → tarball stamped `v1.4.0`.
5. Soak that tagged tarball (Gate 2) → promote the **same** tarball to production.
6. On failure → fix-forward on `main`, tag `v1.4.1`, repeat.

The operator no longer has to be sitting on the tagged commit; the tool guarantees the
artifact is the tag.

## 5. Testing / verification

**New smoke test `tests/bundle_checkout_smoke.sh`** — same shape as
`bundle_version_smoke.sh` (source the lib, drive throwaway temp git repos via
`make_repo`, `set -euo pipefail`, subshell per case, temp-dir cleanup trap). Cases:

1. **Latest reachable tag, HEAD ahead** — annotated tag N commits back; inside the
   subshell after `bundle_checkout_release demo`, HEAD is the tag's commit; after the
   subshell exits, the original branch is restored (assert
   `git -C "$d" symbolic-ref --short HEAD` equals the captured original).
2. **Dirty tracked tree** — modify a tracked file → returns nonzero, HEAD unchanged,
   no checkout.
3. **No annotated tag** — returns nonzero with the guidance message.
4. **Override set** — `DEMO_VERSION_OVERRIDE=x bundle_checkout_release demo` returns 0,
   performs no checkout (still on the branch).
5. **Untracked/gitignored file present** — an untracked `.env` is not treated as dirty;
   the checkout proceeds and the file survives.

Subshell isolation is load-bearing: the `EXIT` trap fires on subshell exit and
restores `$d`'s on-disk HEAD, which the parent then asserts.

**Wire into `.github/workflows/self-ci.yml`** next to the existing
`bundle_version_smoke.sh` invocation.

**Drift validators unchanged** — `validate_bundle_lib.py` and
`validate_make_common.py` keep asserting vendored == canonical byte-for-byte; they
enforce propagation (§6).

**Manual check on one consumer** — annotated tag a few commits back on `main`:
`make bundle` builds the tag and returns you to `main`; dirty the tree → `make bundle`
refuses; `make bundle-dev` bundles `date+sha` regardless.

## 6. Rollout (federation-wide, lockstep)

Both canonical files change, and the byte-for-byte drift checks require every adopting
consumer to re-vendor in lockstep — same mechanic as prior federation bumps.

1. **Change canonical + release `.github`:** edit `configs/bundle/bundle-lib.sh`
   (§3.1) and `configs/make-common/common.mk` (§3.2); add
   `tests/bundle_checkout_smoke.sh` and wire it into `self-ci.yml`; cut a
   `nos-tromo/.github` release.
2. **Propagate to the 6 image-bundling consumers** — `chorus`, `docint`, `Nextext`,
   `translator`, `vllm-service`, `data-plane`. One small PR each: re-vendor
   `scripts/bundle-lib.sh` **and** `make/common.mk` verbatim, add the one-line guard to
   `scripts/bundle_images.sh`, and repoint the workflow ref if the rollout uses an
   immutable pin. Because `common.mk` also changes, both drift checks go red until each
   PR lands (behaviorally harmless — the new target/function are additive to
   already-working repos).
3. **Docs (public, neutral language):** `deploy/README.md` release-ritual runbook (§4);
   each shipping repo's `CLAUDE.md` "Release" note (`make bundle` = latest tag,
   `make bundle-dev` = local state); `infra/CLAUDE.md` shared-conventions update.
   Amend `infra/docs/2026-07-02-federation-release-workflow-design.md` §4.3–§4.4 with a
   pointer to this doc (local-only).

**Open sub-decision (carried from the federation doc):** moving `@v3` alias vs.
immutable `@vX.Y` pin. Since the change is additive, the red drift-window carries no
behavioral risk; recommend the batch mechanic unless a zero-red-window rollout is
wanted, in which case switch consumers to immutable pins so each bumps-pins-re-vendors
in one green PR.

## 7. Error handling / rollback

- **No reachable tag / dirty tree** → `make bundle` refuses with an actionable message
  pointing at `git tag` / `make bundle-dev`. Nothing is built or checked out.
- **Build fails after checkout** → `set -euo pipefail` exits the orchestrator; the
  `EXIT` trap restores the original branch. No stranded detached HEAD.
- **Canonical bump breaks a consumer** → byte-for-byte revert of that repo's vendored
  file(s); blast radius is one repo.
- **A repo not ready to release** keeps using `make bundle-dev` indefinitely; it only
  needs `make bundle` to work once it cuts its first annotated tag.

## 8. Files to change (concrete)

**`nos-tromo/.github` (canonical):**
- `configs/bundle/bundle-lib.sh` — add `bundle_checkout_release()` (§3.1).
- `configs/make-common/common.mk` — add `bundle-dev` target + `.PHONY` + header note (§3.2).
- `tests/bundle_checkout_smoke.sh` — new smoke test (§5).
- `.github/workflows/self-ci.yml` — run the new smoke test.
- `docs/superpowers/specs/2026-07-03-bundle-release-checkout-design.md` — this doc.

**Each image-bundling consumer (`chorus`, `docint`, `Nextext`, `translator`,
`vllm-service`, `data-plane`):**
- `scripts/bundle-lib.sh` — re-vendor to match canonical.
- `make/common.mk` — re-vendor to match canonical.
- `scripts/bundle_images.sh` — add the one-line dev-guarded checkout call.
- CI workflow ref — repoint if switching to an immutable pin.
- `CLAUDE.md` — `bundle` / `bundle-dev` release note.

**`deploy`:** `README.md` — release-ritual runbook (neutral language).

**`infra`:** `CLAUDE.md` (shared conventions) and
`docs/2026-07-02-federation-release-workflow-design.md` (§4.3–§4.4 amendment pointer).

## 9. Out of scope / future

- **`open-webui-service`** tag-based bundling — bespoke Makefile, separate optional
  change.
- **Auto-stash of a dirty tree**, tag auto-push, artifact signing, changelog
  automation — deliberately excluded.
- **Resetting `.<repo>-version` after a production bundle** — it holds the tag until
  the next `bundle`/`bundle-dev`/`build` rewrites it; the file is gitignored and
  transient, and this is pre-existing behavior. Left as-is.
