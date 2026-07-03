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
