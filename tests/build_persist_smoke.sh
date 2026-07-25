#!/usr/bin/env bash
# Behavioral guard for the `build` target in configs/make-common/common.mk (#36):
# build computes a version (explicit <REPO_UC>_VERSION override wins, else a
# fresh date+sha ignoring any existing .<repo>-version), builds with it, and
# persists it to .<repo>-version on success only. Uses only bash + make + git
# (no test framework), matching the repo's shell-in-CI pattern.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CANON="$ROOT/configs/make-common/common.mk"

fail() { echo "FAIL: $1" >&2; exit 1; }

_TMP_DIRS=()
trap 'for d in "${_TMP_DIRS[@]:-}"; do rm -rf "$d"; done' EXIT

# Sets up a fresh consumer dir with a PATH shim for `docker` that logs its
# argv+env to shim.log and exits 0 (or nonzero, if requested). Echoes the dir.
make_consumer() {
  local shim_exit="${1:-0}"
  local d; d="$(mktemp -d)"
  mkdir -p "$d/make" "$d/bin"
  cp "$CANON" "$d/make/common.mk"
  printf 'REPO := demo\nNETWORKS :=\ninclude make/common.mk\n' > "$d/Makefile"
  git -C "$d" init -q
  git -C "$d" config user.email t@example.com
  git -C "$d" config user.name test
  git -C "$d" commit -q --allow-empty -m init
  cat > "$d/bin/docker" <<EOF
#!/bin/sh
{ echo "ARGS: \$*"; env | grep '^DEMO_VERSION='; } >> "$d/shim.log"
exit $shim_exit
EOF
  chmod +x "$d/bin/docker"
  _TMP_DIRS+=("$d")
  printf '%s' "$d"
}

TAG_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}(-[0-9a-f]{7,})?$'

# Case 1: fresh build persists a date+sha tag, and the shim saw it in its env.
d="$(make_consumer)"; ( cd "$d"
  PATH="$d/bin:$PATH" make build >/dev/null
  [[ -f .demo-version ]] || fail "case1: .demo-version not written"
  tag="$(cat .demo-version)"
  [[ "$tag" =~ $TAG_RE ]] || fail "case1: persisted tag '$tag' doesn't match $TAG_RE"
  grep -q "^DEMO_VERSION=$tag\$" shim.log || fail "case1: shim did not see DEMO_VERSION=$tag in its environment"
)

# Case 2: a pre-seeded stale .demo-version is ignored by build and overwritten.
d="$(make_consumer)"; ( cd "$d"
  echo "1999-01-01-deadbee" > .demo-version
  PATH="$d/bin:$PATH" make build >/dev/null
  tag="$(cat .demo-version)"
  [[ "$tag" != "1999-01-01-deadbee" ]] || fail "case2: stale .demo-version was not overwritten"
  [[ "$tag" =~ $TAG_RE ]] || fail "case2: persisted tag '$tag' doesn't match $TAG_RE"
)

# Case 3: an explicit <REPO_UC>_VERSION override wins and is what gets persisted.
d="$(make_consumer)"; ( cd "$d"
  PATH="$d/bin:$PATH" DEMO_VERSION=9.9.9 make build >/dev/null
  tag="$(cat .demo-version)"
  [[ "$tag" == "9.9.9" ]] || fail "case3: expected persisted tag 9.9.9, got '$tag'"
  grep -q '^DEMO_VERSION=9.9.9$' shim.log || fail "case3: shim did not see DEMO_VERSION=9.9.9 in its environment"
)

# Case 4: a failing build (nonzero compose exit) leaves any pre-existing file
# untouched -- the persist line must not run when the build line fails.
d="$(make_consumer 1)"; ( cd "$d"
  echo "2000-01-01-cafebee" > .demo-version
  if PATH="$d/bin:$PATH" make build >/dev/null 2>&1; then
    fail "case4: make build unexpectedly succeeded despite a failing docker shim"
  fi
  tag="$(cat .demo-version)"
  [[ "$tag" == "2000-01-01-cafebee" ]] || fail "case4: pre-existing .demo-version was modified after a failed build (got '$tag')"
)

echo "OK: build persists the built tag to .<repo>-version (override > fresh date+sha; failed build leaves the file untouched)"
