# Bundle Release Checkout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the airgap bundle flow so `make bundle` always produces a tag-versioned production artifact built from the tagged tree, and `make bundle-dev` bundles the current working tree for dev/soak.

**Architecture:** One new shell function (`bundle_checkout_release`) in canonical `configs/bundle/bundle-lib.sh` resolves the latest annotated tag reachable from HEAD, checks it out (detached) before the compose build, and restores the branch via an `EXIT` trap. Canonical `configs/make-common/common.mk` gains a `bundle-dev` target mirroring `up`/`up-dev`. Each of the six image-bundling consumers re-vendors both files byte-for-byte and adds a one-line dev-guarded call. `bundle_version()` is unchanged — after checkout, HEAD is exactly on the tag, so its existing exact-match branch returns the tag.

**Tech Stack:** Bash + git (shell library and smoke tests), GNU Make (lifecycle targets), Python 3.11 stdlib (existing drift validators — not modified), GitHub Actions (self-ci).

## Global Constraints

- **Vendored files are byte-for-byte copies of canonical.** Never hand-edit a consumer's `scripts/bundle-lib.sh` or `make/common.mk`; change the canonical file in `nos-tromo/.github` and re-vendor. Drift is CI-enforced (`validate_bundle_lib.py`, `validate_make_common.py`).
- **The `*-aligned` fixtures must equal canonical** (`tests/fixtures/bundle-aligned/scripts/bundle-lib.sh`, `tests/fixtures/mk-aligned/make/common.mk`). Re-sync them whenever canonical changes. **Never** modify the `*-drifted` fixtures — they are deliberately different to prove the validator catches drift.
- **Annotated tags only.** `git describe` without `--tags` (annotated-only) is the release-identity convention; a lightweight tag must never become a release version.
- **Smoke tests use bash + git only** (no test framework), matching `tests/bundle_version_smoke.sh`, and are invoked via `bash tests/<name>.sh`.
- **Public docs use neutral language** — no host topology, no airgap/hand-carry mechanics, no machine roles, no problem domain (per §0 of the federation release design).
- **Exact consumer slugs** (the arg already passed to `bundle_version` in each repo): `chorus`, `docint`, `nextext`, `translator`, `vllm-service`, `data-plane`.
- **Working directory** for Phase 1 is the `.github` repo on branch `feature/bundle-release-checkout` (the spec is already committed there as `c479ca0`).

---

## Phase 1 — Canonical `.github` changes (branch `feature/bundle-release-checkout`)

### Task 1: `bundle_checkout_release()` + behavioral smoke test

**Files:**
- Create: `tests/bundle_checkout_smoke.sh`
- Modify: `configs/bundle/bundle-lib.sh` (append one function after `bundle_version`, before `bundle_retag`)
- Modify: `tests/fixtures/bundle-aligned/scripts/bundle-lib.sh` (re-sync to canonical)
- Modify: `.github/workflows/self-ci.yml` (append a smoke-test step to the `bundle-lib-smoke` job)

**Interfaces:**
- Produces: `bundle_checkout_release <repo-slug>` — sourced from `bundle-lib.sh`. On success (production path): HEAD is detached on the latest annotated tag reachable from HEAD, an `EXIT` trap restores the original branch, and the global `_BUNDLE_ORIG_REF` holds the original ref. Returns non-zero (no checkout, no trap) when: the tree has uncommitted tracked changes, or no annotated tag is reachable. Returns `0` immediately (no checkout) when `<REPO_UC>_VERSION_OVERRIDE` is set. Consumed by each repo's `scripts/bundle_images.sh` (Task 4).

- [ ] **Step 1: Write the failing smoke test**

Create `tests/bundle_checkout_smoke.sh`:

```bash
#!/usr/bin/env bash
# Behavioral guard for bundle_checkout_release() in configs/bundle/bundle-lib.sh.
# Production `make bundle` resolves the latest annotated tag reachable from HEAD,
# checks it out (detached), and restores the original branch on exit. It refuses
# on a dirty tracked tree or when no annotated tag is reachable; an explicit
# <REPO_UC>_VERSION_OVERRIDE short-circuits it (dev escape hatch). Untracked /
# gitignored files (e.g. .env) are ignored by the dirty check and preserved.
# Bash + git only, matching bundle_version_smoke.sh.
set -euo pipefail

LIB="$(cd "$(dirname "$0")/.." && pwd)/configs/bundle/bundle-lib.sh"
# shellcheck source=/dev/null
source "$LIB"

fail() { echo "FAIL: $1" >&2; exit 1; }

_TMP_REPOS=()
trap 'for d in "${_TMP_REPOS[@]:-}"; do rm -rf "$d"; done' EXIT

# A repo on branch `main` with an annotated tag v1.0.0 one commit behind HEAD.
make_repo_tagged() {
  local d; d="$(mktemp -d)"
  git -C "$d" init -q -b main
  git -C "$d" config user.email t@example.com
  git -C "$d" config user.name test
  git -C "$d" commit -q --allow-empty -m first
  git -C "$d" tag -a v1.0.0 -m release
  git -C "$d" commit -q --allow-empty -m second   # HEAD now ahead of the tag
  _TMP_REPOS+=("$d")
  printf '%s' "$d"
}

# Case 1: latest reachable tag is checked out, branch restored on subshell exit.
d="$(make_repo_tagged)"
(
  cd "$d"
  bundle_checkout_release demo
  head_sha="$(git rev-parse HEAD)"
  tag_sha="$(git rev-list -n 1 v1.0.0)"        # annotated tag -> its commit
  [[ "$head_sha" == "$tag_sha" ]] || fail "case1: expected HEAD at v1.0.0, got $head_sha"
)
now="$(git -C "$d" symbolic-ref --quiet --short HEAD || echo DETACHED)"
[[ "$now" == "main" ]] || fail "case1: expected branch main restored, got $now"

# Case 2: dirty tracked tree -> refused (nonzero), HEAD unmoved.
d="$(make_repo_tagged)"
(
  cd "$d"
  echo v1 > tracked.txt; git add tracked.txt; git commit -q -m "add tracked"
  echo v2 >> tracked.txt                     # unstaged change to a tracked file
  if bundle_checkout_release demo 2>/dev/null; then
    fail "case2: expected refusal on a dirty tracked tree"
  fi
  [[ "$(git symbolic-ref --short HEAD)" == "main" ]] || fail "case2: HEAD moved despite refusal"
)

# Case 3: no annotated tag reachable -> refused (nonzero).
d="$(mktemp -d)"; _TMP_REPOS+=("$d")
(
  cd "$d"
  git init -q -b main
  git config user.email t@example.com; git config user.name test
  git commit -q --allow-empty -m only
  if bundle_checkout_release demo 2>/dev/null; then
    fail "case3: expected refusal when no annotated tag is reachable"
  fi
)

# Case 4: explicit override short-circuits -> success, no checkout.
d="$(make_repo_tagged)"
(
  cd "$d"
  DEMO_VERSION_OVERRIDE=custom-1 bundle_checkout_release demo
  [[ "$(git symbolic-ref --short HEAD)" == "main" ]] || fail "case4: override should not check out a tag"
)

# Case 5: untracked/gitignored file is NOT dirty -> checkout proceeds, file survives.
d="$(make_repo_tagged)"
(
  cd "$d"
  echo SECRET > .env                         # untracked (never git-added)
  bundle_checkout_release demo               # must not refuse
  [[ -f .env ]] || fail "case5: .env must survive the checkout"
)
[[ "$(git -C "$d" symbolic-ref --quiet --short HEAD)" == "main" ]] || fail "case5: branch not restored"

echo "OK: bundle_checkout_release (resolve latest tag, checkout+restore, refuse dirty/no-tag, override + untracked-safe)"
```

- [ ] **Step 2: Run the smoke test to verify it fails**

Run: `bash tests/bundle_checkout_smoke.sh`
Expected: FAIL — `bundle_checkout_release: command not found` (the function does not exist yet), non-zero exit.

- [ ] **Step 3: Implement `bundle_checkout_release` in canonical `bundle-lib.sh`**

In `configs/bundle/bundle-lib.sh`, add this function immediately after the closing `}` of `bundle_version` (currently line 66) and before `bundle_retag` (currently line 68):

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
  # matching bundle_version's --exact-match convention).
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

Also add `_BUNDLE_ORIG_REF` to the `shellcheck disable=SC2034` note at the top of the file if shellcheck flags it as unused. The current top-of-file directive is:
```bash
# shellcheck disable=SC2034  # BUNDLE_VERSION/BUNDLE_BUILT/BUNDLE_PULLED are set for the sourcing bundle_images.sh
```
Extend the comment to mention `_BUNDLE_ORIG_REF` is consumed by the deferred EXIT trap:
```bash
# shellcheck disable=SC2034  # BUNDLE_VERSION/BUNDLE_BUILT/BUNDLE_PULLED/_BUNDLE_ORIG_REF are set for the sourcing bundle_images.sh (the last is read by the EXIT trap)
```

- [ ] **Step 4: Run the smoke test to verify it passes**

Run: `bash tests/bundle_checkout_smoke.sh`
Expected: PASS — prints `OK: bundle_checkout_release (...)`, exit 0.

- [ ] **Step 5: Re-sync the aligned bundle fixture to canonical**

Run: `cp configs/bundle/bundle-lib.sh tests/fixtures/bundle-aligned/scripts/bundle-lib.sh`

- [ ] **Step 6: Verify the drift validator still passes aligned and fails drifted**

Run:
```bash
python3 scripts/validate_bundle_lib.py --consumer-root tests/fixtures/bundle-aligned
python3 scripts/validate_bundle_lib.py --consumer-root tests/fixtures/bundle-drifted; echo "drifted exit=$?"
```
Expected: aligned prints `scripts/bundle-lib.sh alignment check OK.` (exit 0); drifted prints a FAILED diff and `drifted exit=1`.

- [ ] **Step 7: Wire the smoke test into self-ci**

In `.github/workflows/self-ci.yml`, in the `bundle-lib-smoke` job, after the existing final step (`- name: bundle_version behavior (override > annotated tag > date+sha)` / `run: bash tests/bundle_version_smoke.sh`, ending line 142), append:

```yaml
      - name: bundle_checkout_release behavior (resolve tag, checkout+restore, refuse dirty/no-tag)
        run: bash tests/bundle_checkout_smoke.sh
```

- [ ] **Step 8: Commit**

```bash
git add configs/bundle/bundle-lib.sh tests/bundle_checkout_smoke.sh \
        tests/fixtures/bundle-aligned/scripts/bundle-lib.sh .github/workflows/self-ci.yml
git commit -m "$(cat <<'EOF'
feat(bundle-lib): add bundle_checkout_release for production make bundle

Resolves the latest annotated tag reachable from HEAD, checks it out so the
compose build is the tagged tree, and restores the branch via an EXIT trap.
Refuses on a dirty tracked tree or when no annotated tag is reachable; an
explicit <REPO_UC>_VERSION_OVERRIDE short-circuits it (dev escape hatch).
Guarded by tests/bundle_checkout_smoke.sh in self-ci; aligned fixture re-synced.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `bundle-dev` target in canonical `common.mk`

**Files:**
- Modify: `configs/make-common/common.mk` (`.PHONY` line 46, header comment block lines 9–12, add target after `bundle` at line 63)
- Modify: `tests/fixtures/mk-aligned/make/common.mk` (re-sync to canonical)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `make bundle` (unchanged recipe, `./scripts/bundle_images.sh`) and `make bundle-dev` (`BUNDLE_DEV=1 ./scripts/bundle_images.sh`). The `BUNDLE_DEV` env var is the flag each consumer's `bundle_images.sh` reads to skip the checkout (Task 4).

- [ ] **Step 1: Add `bundle-dev` to `.PHONY`**

In `configs/make-common/common.mk` line 46, change:
```make
.PHONY: network volumes build bundle up up-dev dev stop down logs pre-commit
```
to:
```make
.PHONY: network volumes build bundle bundle-dev up up-dev dev stop down logs pre-commit
```

- [ ] **Step 2: Add the `bundle-dev` target and a comment**

In `configs/make-common/common.mk`, replace the current `bundle` target (lines 62–63):
```make
bundle:
	./scripts/bundle_images.sh
```
with:
```make
# Airgap release artifact. `bundle` is PRODUCTION: it builds the latest annotated
# tag reachable from HEAD (checks it out, builds, restores your branch) and
# refuses on a dirty tree or when no tag is reachable. `bundle-dev` bundles the
# current working tree as-is (date+sha / override) for dev iteration and soak.
bundle:
	./scripts/bundle_images.sh

bundle-dev:
	BUNDLE_DEV=1 ./scripts/bundle_images.sh
```

- [ ] **Step 3: Update the header comment block**

In `configs/make-common/common.mk`, the header note about `up`/`up-dev` (lines 9–12) currently reads:
```make
# `up` / `up-dev` are detached and never build: they run `up -d --no-build`
# (production shape), matching the bespoke pulled-image members (data-plane,
# open-webui-service). Build first, then bring up: `make build && make up-dev`
# in dev (or just `make dev`); load/pull images before `make up` in prod.
```
Append one line after it:
```make
# `bundle` builds the latest reachable annotated tag (production); `bundle-dev`
# bundles the current working tree (dev/soak). See scripts/bundle-lib.sh.
```

- [ ] **Step 4: Re-sync the aligned make-common fixture to canonical**

Run: `cp configs/make-common/common.mk tests/fixtures/mk-aligned/make/common.mk`

- [ ] **Step 5: Verify the drift validator, and that both targets expand correctly**

Run:
```bash
python3 scripts/validate_make_common.py --consumer-root tests/fixtures/mk-aligned
tmp="$(mktemp -d)"; mkdir -p "$tmp/make"
cp configs/make-common/common.mk "$tmp/make/common.mk"
printf 'REPO := demo\nNETWORKS := inference-net\ninclude make/common.mk\n' > "$tmp/Makefile"
make -C "$tmp" -n bundle
make -C "$tmp" -n bundle-dev
rm -rf "$tmp"
```
Expected: validator prints `make/common.mk alignment check OK.` (exit 0); `make -n bundle` prints `./scripts/bundle_images.sh`; `make -n bundle-dev` prints `BUNDLE_DEV=1 ./scripts/bundle_images.sh`.

- [ ] **Step 6: Commit**

```bash
git add configs/make-common/common.mk tests/fixtures/mk-aligned/make/common.mk
git commit -m "$(cat <<'EOF'
feat(make-common): add bundle-dev target (current tree) alongside bundle

`bundle` stays the production recipe (latest tag via bundle_checkout_release);
`bundle-dev` sets BUNDLE_DEV=1 to bundle the current working tree, mirroring the
up / up-dev pair. Aligned fixture re-synced.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Open the `.github` PR and cut the config release

This task gates all of Phase 2 — consumers re-vendor from the *released* canonical.

- [ ] **Step 1: Push the branch and open the PR**

```bash
git push -u origin feature/bundle-release-checkout
gh pr create --repo nos-tromo/.github --base main \
  --title "feat: make bundle builds latest tag; add bundle-dev (bundle-release-checkout)" \
  --body "$(cat <<'EOF'
Implements docs/superpowers/specs/2026-07-03-bundle-release-checkout-design.md.

- `bundle_checkout_release()` in configs/bundle/bundle-lib.sh: production `make
  bundle` resolves the latest annotated tag reachable from HEAD, checks it out,
  builds the tagged tree, restores the branch; refuses on a dirty tree / no tag.
- `bundle-dev` target in configs/make-common/common.mk: bundle the current tree.
- New tests/bundle_checkout_smoke.sh wired into self-ci; aligned fixtures re-synced.

Consumer re-vendor PRs follow once this is released.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: After review + green self-ci, merge to `main`**

Merge via the GitHub UI or `gh pr merge --squash` (match the repo's convention). Confirm the `self-ci` checks (`bundle-lib-smoke`, `make-common-smoke`) are green.

- [ ] **Step 3: Determine the next release version**

Run: `git checkout main && git pull && git tag -l 'v3*' --sort=-v:refname | head -3`
This is an additive, backward-compatible change → bump the **minor**: if the latest is `v3.2`, the new tag is `v3.3`. Substitute the actual next value below as `<vX.Y>`.

- [ ] **Step 4: Cut the annotated release tag and move the `@v3` alias**

```bash
git tag -a <vX.Y> -m "make bundle builds latest tag; add bundle-dev"
git push origin <vX.Y>
# Move the moving major alias consumers pin (matches the federation bump playbook):
git tag -f -a v3 -m "v3 -> <vX.Y>"
git push -f origin v3
```
Expected: `git tag -l | grep -E '^v3'` shows both `<vX.Y>` and the moved `v3`.

---

## Phase 2 — Consumer rollout (six repos, gated on the `.github` release)

### Task 4: Re-vendor + guard + release note, per consumer

Run this identical sequence once **per repo** in the table, substituting `<repo-dir>` and `<slug>`. Each repo is a separate git repository under `infra/`; each gets its own branch, commit, and PR. All paths below are relative to the repo root; the canonical source is the sibling `../.github` checkout on `main` (post-release).

| `<repo-dir>` | `<slug>` | Shape |
|---|---|---|
| `chorus` | `chorus` | build + save |
| `docint` | `docint` | build + save |
| `Nextext` | `nextext` | build + save (note: dir is `Nextext`, slug is lowercase `nextext`) |
| `translator` | `translator` | build + save |
| `vllm-service` | `vllm-service` | build + save |
| `data-plane` | `data-plane` | pull-only, **bespoke Makefile** — see exception below |

> **`data-plane` exception (discovered during rollout):** it keeps a bespoke Makefile
> that does **not** `include make/common.mk` (same class as `open-webui-service`). So it
> re-vendors **`scripts/bundle-lib.sh` only** (not `common.mk`), adds the guard line, and
> gets a doc note in CLAUDE.md **and** README.md — **no `common.mk`, no `bundle-dev`
> target**. Its dev escape hatch is `DATA_PLANE_VERSION_OVERRIDE` (which
> `bundle_checkout_release` honors). Skip the `make/common.mk` cp and the
> `make -n bundle-dev` check for this repo; `validate_make_common.py` legitimately skips
> it (not adopted).

**Files (per repo):**
- Modify: `scripts/bundle-lib.sh` (re-vendor)
- Modify: `make/common.mk` (re-vendor)
- Modify: `scripts/bundle_images.sh` (add one guard line)
- Modify: `CLAUDE.md` (release note)

**Interfaces:**
- Consumes: `bundle_checkout_release <slug>` from the re-vendored `scripts/bundle-lib.sh` (Task 1); `make bundle-dev` from the re-vendored `make/common.mk` (Task 2).

- [ ] **Step 1: Branch**

```bash
cd ../<repo-dir>
git checkout main && git pull
git checkout -b feature/bundle-release-checkout
```

- [ ] **Step 2: Re-vendor both canonical files byte-for-byte**

```bash
cp ../.github/configs/bundle/bundle-lib.sh scripts/bundle-lib.sh
cp ../.github/configs/make-common/common.mk make/common.mk
```

- [ ] **Step 3: Add the dev-guarded checkout call to `scripts/bundle_images.sh`**

Insert one line **immediately before** the existing `bundle_version <slug>` line:

```bash
[[ -n "${BUNDLE_DEV:-}" ]] || bundle_checkout_release <slug>
```

Concrete example — `scripts/bundle_images.sh` in `chorus` becomes (added line marked):
```bash
. scripts/bundle-lib.sh

COMPOSE=(docker compose --env-file .env -f docker/compose.yaml)
[[ -n "${BUNDLE_DEV:-}" ]] || bundle_checkout_release chorus   # <-- added
bundle_version chorus; VER="$BUNDLE_VERSION"

"${COMPOSE[@]}" build
bundle_partition_images < <("${COMPOSE[@]}" config --images)
```
For `data-plane`, the same one line goes immediately before `bundle_version data-plane` (which sits before the `COMPOSE=(...)` / `bundle_collect_pulled` lines) — the checkout precedes the `config --images` read, so the tagged `compose.yaml`/`.env` define the pulled versions.

- [ ] **Step 4: Add a Release note to `CLAUDE.md`**

Add (or extend an existing "Release"/"Commands" section in) `CLAUDE.md` with this neutral-language note:

```markdown
## Release

- `make bundle` — **production** artifact: builds the latest annotated tag
  reachable from `HEAD` (checks it out, builds, restores your branch) and
  refuses on a dirty tree or when no tag is reachable. The image is stamped
  `vX.Y.Z`.
- `make bundle-dev` — bundles the current working tree as-is (`date+sha`, or
  `<REPO_UC>_VERSION_OVERRIDE`) for dev iteration and staging soak.
```

- [ ] **Step 5: Verify alignment locally**

```bash
python3 ../.github/scripts/validate_bundle_lib.py
python3 ../.github/scripts/validate_make_common.py
make -n bundle-dev
```
Expected: both validators print their `... alignment check OK.` line (exit 0); `make -n bundle-dev` prints `BUNDLE_DEV=1 ./scripts/bundle_images.sh`.

- [ ] **Step 6: Commit, push, PR**

```bash
git add scripts/bundle-lib.sh make/common.mk scripts/bundle_images.sh CLAUDE.md
git commit -m "$(cat <<'EOF'
feat: adopt bundle-release-checkout — make bundle builds latest tag; add bundle-dev

Re-vendor scripts/bundle-lib.sh and make/common.mk from nos-tromo/.github, and
call bundle_checkout_release in scripts/bundle_images.sh (dev-guarded). `make
bundle` now builds the latest annotated tag; `make bundle-dev` bundles the
current tree.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
git push -u origin feature/bundle-release-checkout
gh pr create --base main --title "feat: adopt bundle-release-checkout" \
  --body "Re-vendor bundle-lib.sh + common.mk from the .github <vX.Y> release; add the dev-guarded bundle_checkout_release call and a CLAUDE.md release note.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 7: Confirm CI green and merge**

Confirm each repo's reusable CI (which runs `validate_make_common.py` + `validate_bundle_lib.py` against the pinned `@v3` ref) is green, then merge. Repeat Steps 1–7 for every row in the table.

---

## Phase 3 — Cross-cutting docs

### Task 5: Runbook + workspace docs + supersession pointer

**Files:**
- Modify: `deploy/README.md` (release-ritual runbook — public, neutral)
- Modify: `infra/CLAUDE.md` (shared conventions — local; specifics permitted)
- Modify: `infra/docs/2026-07-02-federation-release-workflow-design.md` (§4.3–§4.4 amendment pointer — local)

- [ ] **Step 1: Update the `deploy/README.md` release ritual (neutral language)**

In `deploy/README.md`, in the "Releasing" section, replace the bundle step so the ritual reads (keep surrounding host/airgap specifics out — neutral only):

```markdown
2. Tag the release on `main`: `git tag -a vX.Y.Z -m "…"` and push the tag.
3. `make bundle` in the target repo — from anywhere on that history — resolves
   the latest annotated tag, builds it, and stamps the artifact `vX.Y.Z`. (Use
   `make bundle-dev` only for pre-tag iteration/soak; it stamps `date+sha` and
   is never promoted.)
```

- [ ] **Step 2: Update `infra/CLAUDE.md`**

In `infra/CLAUDE.md`, in the "Release workflow (federation-wide)" section, update the `make bundle` bullet to distinguish the two targets:

```markdown
- `make bundle` builds the **latest annotated tag reachable from HEAD** (checks
  it out, builds, restores your branch) and refuses on a dirty tree or when no
  tag is reachable — production releases are always a versioned, immutable tag.
  `make bundle-dev` bundles the current working tree (`date+sha`/override) for
  dev iteration and staging soak. Implemented in the shared
  `bundle_checkout_release` (`nos-tromo/.github` → `configs/bundle/bundle-lib.sh`).
```

- [ ] **Step 3: Add the supersession pointer to the federation design doc**

At the top of `infra/docs/2026-07-02-federation-release-workflow-design.md` §4.3, add a one-line note:

```markdown
> **Superseded (2026-07-03):** the on-tag-only / date+sha-fallback mechanic below
> is replaced by `bundle_checkout_release` (production `make bundle` resolves and
> checks out the latest reachable tag) + a `make bundle-dev` for the current tree.
> See `2026-07-03-bundle-release-checkout-design.md`. The GitHub-Flow branching
> and Gate-2 soak ritual are unchanged.
```

- [ ] **Step 4: Commit the `deploy` doc**

`deploy` is its own repo — branch, commit, PR:
```bash
cd ../deploy && git checkout main && git pull && git checkout -b docs/bundle-release-checkout
git add README.md
git commit -m "$(cat <<'EOF'
docs: release ritual — make bundle builds latest tag; bundle-dev for local soak

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
git push -u origin docs/bundle-release-checkout
gh pr create --base main --title "docs: bundle-release-checkout release ritual" \
  --body "Neutral-language runbook update for make bundle / make bundle-dev.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 5: Note the local-only edits**

`infra/CLAUDE.md` and `infra/docs/2026-07-02-federation-release-workflow-design.md` live in the non-repo `infra/` root — save the edits in place; there is nothing to push. State this in the execution summary so the local edits are not mistaken for uncommitted repo changes.

---

## Self-Review

**Spec coverage** (each spec section → task):
- §3.1 `bundle_checkout_release` → Task 1 ✓
- §3.2 `common.mk` `bundle-dev` → Task 2 ✓
- §3.3 per-repo one-line guard → Task 4 ✓
- §4 release ritual docs → Task 5 (deploy/README) ✓
- §5 smoke test + self-ci wiring + aligned-fixture sync → Task 1 (bundle) & Task 2 (mk) ✓
- §6 rollout: canonical release → Task 3; six consumer PRs → Task 4; docs → Task 5 ✓
- §7 error handling (dirty/no-tag refusal, trap restore) → Task 1 function + smoke cases 2/3 ✓
- §8 files-to-change → all mapped across Tasks 1–5 ✓; per-repo `CLAUDE.md` folded into Task 4, `infra/CLAUDE.md` + federation amendment into Task 5 ✓
- §9 out-of-scope (`open-webui-service`, auto-stash, `.<repo>-version` reset) → not implemented, as intended ✓

**Placeholder scan:** No TBD/TODO. `<repo-dir>`, `<slug>`, `<vX.Y>`, `vX.Y.Z` are explicit substitution variables (repo table gives every `<repo-dir>`/`<slug>`; Task 3 Step 3 derives `<vX.Y>`) — not unfilled placeholders.

**Type/name consistency:** `bundle_checkout_release <slug>` and the `BUNDLE_DEV` flag and `_BUNDLE_ORIG_REF` global are named identically in the function (Task 1), the `bundle-dev` recipe (Task 2), and the consumer guard (Task 4). Smoke test asserts the same behaviors the function implements. Slugs in the Task 4 table match the verified `bundle_version <slug>` args.
