# Build-Persists-Version (#36) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `make build` persists the tag it builds to `.<repo>-version`, so `up` can never reference an unbuilt tag (nos-tromo/.github#36).

**Architecture:** Per the approved spec (`docs/superpowers/specs/2026-07-25-build-persists-version-design.md`): one canonical change in `configs/make-common/common.mk` — a `BUILD_VERSION` that honors an explicit `<REPO_UC>_VERSION` override (detected via `$(origin)`) and otherwise computes a fresh `date+sha` ignoring any stale file; the `build` recipe builds with it and persists it on success. Then release v3.9 and re-vendor byte-identically into the five consumers (chorus, docint, Nextext, translator, vllm-service), adding the missing `.{repo}-version` gitignore where absent.

**Tech Stack:** GNU Make, bash, docker compose; per-repo CI (drift check `scripts/validate_make_common.py`).

## Global Constraints

- Data confidentiality (hard rule): no real data / absolute local paths in anything committed, in any repo.
- The `.<repo>-version` file's meaning after this change: *the image version currently deployable on this host* — written by `build` and by `bundle`/`bundle-dev`, read by everything else via the existing `?=` chain. Do not change the `?=` chain, `up`/`up-dev` (`--no-build` stays), or anything in `scripts/bundle-lib.sh`.
- An exported `<REPO_UC>_VERSION` must keep winning in `build`, and what wins is what gets persisted.
- Vendored copies must be byte-identical to canonical (CI drift check).
- PRs everywhere; CI green; do NOT merge any PR (user merges). Tag v3.9 only AFTER the canonical PR is merged (hand-annotated tag, the repo's cadence).
- Branch in `.github`: `fix/build-persists-version` (exists, carries the spec).

---

### Task 1: canonical common.mk change + PR

**Files (the `.github` repo in the infra workspace, branch `fix/build-persists-version`):**
- Modify: `configs/make-common/common.mk` (VERSION block comment + new `BUILD_VERSION` + `build` recipe)
- Inspect (extend only if a harness already covers common.mk recipes): `.github/workflows/self-ci.yml`

**Interfaces:**
- Produces: the canonical file content that Tasks 2–4 copy/verify verbatim; `BUILD_VERSION` (make var), persisted file `.$(REPO)-version`.

- [ ] **Step 1: Comment update.** In the VERSION block, change the line
  `# Production: read .<repo>-version (written by bundle.sh).` region so the file's contract is stated once:

```makefile
# Versioned image tag. `.$(REPO)-version` names the version currently
# deployable on this host — written by `build` (fresh date+sha, or an
# explicit override) and by the bundle path; read first by everything
# else. Dev fallback when the file is absent: YYYY-MM-DD[-<short-sha>].
# Override by exporting <REPO_UC>_VERSION.
```

(Keep the existing `$(eval ...)`/`export` lines byte-identical.)

- [ ] **Step 2: BUILD_VERSION + build recipe.** Directly above the `build:` target add:

```makefile
# Version to build: an explicit <REPO_UC>_VERSION override (environment or
# command line) wins; otherwise compute a FRESH date+sha, deliberately
# ignoring any existing .$(REPO)-version — a stale file must not stamp an
# old tag name onto new content. `build` persists what it built (#36), so
# later `up`/`up-dev` (which read the file first) always reference the
# last tag actually built on this host, immune to date/commit rollover.
BUILD_VERSION = $(if $(filter environment command,$(firstword $(origin $(VERSION_VAR)))),$($(VERSION_VAR)),$(shell _s=$$(git rev-parse --short HEAD 2>/dev/null); echo "$$(date +%Y-%m-%d)$${_s:+-$$_s}"))
```

and replace the `build` recipe:

```makefile
build:
	@echo ">> building $(REPO) $(BUILD_VERSION)"
	$(BUILD_ENV) $(VERSION_VAR)="$(BUILD_VERSION)" $(COMPOSE) build
	@printf '%s\n' "$(BUILD_VERSION)" > .$(REPO)-version
```

Notes for the implementer: `$(origin)` returns `environment`, `environment override`, or `command line` for caller-supplied values and `file` for the `?=`-eval'd default — `$(firstword ...)` + `$(filter environment command,...)` covers exactly the caller cases. The recipe's second line exports the chosen version into the compose invocation's environment (shell env beats `.env` in compose interpolation). The `printf` runs only if the build line succeeded (separate recipe lines abort on failure). `BUILD_VERSION` uses recursive `=`, so each recipe line expands it independently — harmless here, because the value can only change on a date rollover or a new commit, neither of which happens between adjacent recipe lines; keep the three-line form.

- [ ] **Step 3: Sanity-check with a scratch harness.** In the scratchpad (NOT in the repo), create a minimal consumer and exercise the semantics with `make -n`/fake compose:

```bash
SCRATCH=$(mktemp -d)   # or the session scratchpad && mkdir -p $SCRATCH/make && cd $SCRATCH
cp ../../.github/configs/make-common/common.mk make/
printf 'REPO := demo\nNETWORKS :=\ninclude make/common.mk\n' > Makefile
git init -q . && git commit -q --allow-empty -m x   # so rev-parse works
printf '#!/bin/sh\necho FAKE-COMPOSE "$@"\n' > docker && chmod +x docker  # not used; use make -n instead
make -n build                          # shows: echo ">> building demo <today>-<sha>" etc.
echo "1999-01-01-deadbee" > .demo-version
make -n build | grep -c 1999           # expect 0 — stale file ignored by build
DEMO_VERSION=9.9.9 make -n build | grep -c 9.9.9   # expect >=1 — override wins
make -n up                             # interpolation would read .demo-version (file present)
rm -rf $SCRATCH
```

Expected outcomes as annotated. If `$(origin)` handling misbehaves, fix the filter, not the spec.

- [ ] **Step 4: self-ci check.** Read `.github/workflows/self-ci.yml`: if it contains behavioral tests for common.mk recipes (it does for `bundle_version` in bundle-lib), add one analogous step exercising the Step 3 semantics (fresh-ignores-stale-file, override-wins, file-written-on-success can be tested with a stub compose via PATH shim); if the existing harness only covers bundle-lib.sh, add the new step alongside it following its style. Keep it self-contained and fast.

- [ ] **Step 5: Commit + PR.**

```bash
git add configs/make-common/common.mk .github/workflows/self-ci.yml
git commit -m "fix(common.mk): make build persist the built tag to .<repo>-version (#36)"
git push -u origin fix/build-persists-version
gh pr create --title "fix(common.mk): make build persists the built tag (#36)" \
  --body "Closes #36. build computes a fresh date+sha (explicit <REPO_UC>_VERSION override still wins), builds with it, and persists it to .<repo>-version on success — the same file the bundle path writes — so up/up-dev always reference the last tag actually built on this host instead of recomputing from today's date + HEAD (which stranded containers across day/commit boundaries under --no-build). Spec: docs/superpowers/specs/2026-07-25-build-persists-version-design.md. Live-verified on translator (see PR comment)."
gh pr checks --watch
```

CI green; do not merge.

---

### Task 2: live verification on translator (pre-merge evidence)

**Files:** none committed — local-only verification in the sibling `translator` repo using the canonical file from Task 1's branch.

**Interfaces:**
- Consumes: Task 1's `configs/make-common/common.mk`.
- Produces: evidence posted as a comment on the Task 1 PR.

- [ ] **Step 1:** In translator (clean tree on main): `cp ../.github/configs/make-common/common.mk make/common.mk` (LOCAL ONLY — do not commit; the drift check would fail until v3.9 lands).
- [ ] **Step 2 (spec verification 1):** `make build` → completes; `cat .translator-version` equals the tag of the images just built (`docker images | grep translator- | head` shows the same tag).
- [ ] **Step 3 (spec verification 2):** `echo 1999-01-01-deadbee > .translator-version && make build` → `.translator-version` now holds the fresh real tag, not 1999.
- [ ] **Step 4 (spec verification 3, the healed failure):** `docker compose --env-file .env -f docker/compose.yaml config | grep 'image:'` → tags equal the file's value (file wins over recomputation); `make up` → containers recreate/no-op against the existing image; stack healthy (`docker ps` shows translator backend+frontend up; authenticated gateway check `/translator/api/v1/languages` → 200 optional but nice).
- [ ] **Step 5 (spec verification 4, fresh-clone shape):** `mv .translator-version "$TMPDIR"/tv.bak && docker compose --env-file .env -f docker/compose.yaml config | grep 'image:'` → tag falls back to computed date+sha; `mv "$TMPDIR"/tv.bak .translator-version`. (Any writable temp dir works.)
- [ ] **Step 6:** `git -C . checkout make/common.mk` (restore vendored copy). Post the evidence: `gh pr comment <PR#> -R nos-tromo/.github --body "<verification transcript summary>"`.

**STOP after Task 2: the user merges the canonical PR before Tasks 3–4.**

---

### Task 3: release v3.9

**Files:** none — tag only, after the canonical PR is merged.

- [ ] **Step 1:** `cd /Users/himarc/dev/nos-tromo/infra/.github && git switch main && git pull --ff-only`; confirm the merge landed (`git log --oneline -2` shows the #36 commit).
- [ ] **Step 2:** `git tag -a v3.9 -m "v3.9: common.mk build persists the built tag to .<repo>-version (#36)" && git push origin v3.9` — matching the repo's hand-annotated cadence (see `git tag -n1 v3.8`).

---

### Task 4: re-vendor wave (5 repos)

**Files (per repo, branch `chore/common-mk-v3.9` off pulled main):**
- Modify: `make/common.mk` (byte-identical copy of canonical), `.gitignore` (only where the ignore is missing)

Repos and gitignore status (re-check, don't trust): `chorus` (has ignore), `docint` (MISSING — add `.docint-version`), `Nextext` (MISSING — add `.nextext-version`), `translator` (has), `vllm-service` (has).

Per repo:
- [ ] `git checkout main && git pull && git checkout -b chore/common-mk-v3.9`
- [ ] `cp ../.github/configs/make-common/common.mk make/common.mk` then `diff ../.github/configs/make-common/common.mk make/common.mk` → empty (byte-identical).
- [ ] Where the `.{repo}-version` gitignore line is missing (check `grep -- '-version' .gitignore`), add it with a one-line comment matching the file's style.
- [ ] `make -n build | head -3` → shows the new `>> building <repo> <tag>` line (parse check).
- [ ] `make verify` if the repo has it (the four apps + vllm-service do) — pre-commit is tracked-only, so `git add` first.
- [ ] Commit `chore: vendor common.mk v3.9 — build persists the built tag (#36 upstream)`, push, `gh pr create` with a one-paragraph body linking nos-tromo/.github#36 and the v3.9 tag, `gh pr checks --watch` green (the drift check validates byte-identity against canonical main). Do not merge.
- [ ] Final report: five PR URLs + CI status.
