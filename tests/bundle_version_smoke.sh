#!/usr/bin/env bash
# Behavioral guard for bundle_version() in configs/bundle/bundle-lib.sh.
# Precedence: <REPO_UC>_VERSION_OVERRIDE  >  exact annotated tag  >  date+sha.
# Uses only bash + git (no test framework), matching the repo's shell-in-CI pattern.
set -euo pipefail

LIB="$(cd "$(dirname "$0")/.." && pwd)/configs/bundle/bundle-lib.sh"
# shellcheck source=/dev/null
source "$LIB"

fail() { echo "FAIL: $1" >&2; exit 1; }

make_repo() {
  local d; d="$(mktemp -d)"
  git -C "$d" init -q
  git -C "$d" config user.email t@example.com
  git -C "$d" config user.name test
  git -C "$d" commit -q --allow-empty -m init
  printf '%s' "$d"
}

# Case 1: no tag -> date+sha fallback (contains the short sha)
d="$(make_repo)"; ( cd "$d"
  bundle_version demo >/dev/null
  sha="$(git rev-parse --short HEAD)"
  [[ "$BUNDLE_VERSION" == *"$sha"* ]] || fail "no-tag: expected date+sha with $sha, got $BUNDLE_VERSION"
)

# Case 2: HEAD on an annotated tag -> the tag verbatim
d="$(make_repo)"; ( cd "$d"
  git tag -a v9.9.9 -m release
  bundle_version demo >/dev/null
  [[ "$BUNDLE_VERSION" == "v9.9.9" ]] || fail "annotated-tag: expected v9.9.9, got $BUNDLE_VERSION"
)

# Case 3: override beats even a tag
d="$(make_repo)"; ( cd "$d"
  git tag -a v9.9.9 -m release
  DEMO_VERSION_OVERRIDE=custom-1 bundle_version demo >/dev/null
  [[ "$BUNDLE_VERSION" == "custom-1" ]] || fail "override: expected custom-1, got $BUNDLE_VERSION"
)

# Case 4: lightweight tag is ignored (annotated-only) -> date+sha fallback
d="$(make_repo)"; ( cd "$d"
  git tag v8.8.8   # lightweight (no -a)
  bundle_version demo >/dev/null
  sha="$(git rev-parse --short HEAD)"
  [[ "$BUNDLE_VERSION" == *"$sha"* ]] || fail "lightweight-tag: expected date+sha fallback, got $BUNDLE_VERSION"
)

echo "OK: bundle_version precedence (override > annotated tag > date+sha) holds"
