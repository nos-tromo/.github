#!/usr/bin/env bash
# Behavioral guard for scripts/validate_image_provenance.py against the
# tests/fixtures/prov-* consumers. Stub gh/cosign binaries on PATH stand in
# for the real verifiers, so this needs no network or token; the live checks
# against real registries run as separate self-ci steps.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/validate_image_provenance.py"
POLICY="$ROOT/tests/fixtures/prov-policy/signers.toml"
FIX="$ROOT/tests/fixtures"

fail() { echo "FAIL: $1" >&2; exit 1; }

STUBS="$(mktemp -d)"
trap 'rm -rf "$STUBS"' EXIT

# The fixtures pin this digest wherever a signature should verify.
GOOD="sha256:$(printf 'a%.0s' $(seq 1 64))"
for tool in gh cosign; do
  cat > "$STUBS/$tool" <<EOF
#!/bin/sh
# Stub $tool: verifies only the fixtures' known-good digest. STUB_DOWN makes
# every call fail with STUB_FAILURE, standing in for a service outage.
if [ -z "\${STUB_DOWN:-}" ]; then
  case "\$*" in
    *$GOOD*) echo '[]'; exit 0 ;;
  esac
fi
echo "\${STUB_FAILURE:-Error: HTTP 404: Not Found}" >&2
exit 1
EOF
  chmod +x "$STUBS/$tool"
done

run() { PATH="$STUBS:$PATH" python3 "$SCRIPT" --signers "$POLICY" --consumer-root "$1"; }

# Case 1: every listed image verifies; unlisted pins are reported as unsigned.
out="$(run "$FIX/prov-aligned")" || fail "prov-aligned: expected exit 0"
grep -q "image provenance check OK (3 verified" <<<"$out" || fail "prov-aligned: unexpected output: $out"
grep -q "docker.io/library/python" <<<"$out" || fail "prov-aligned: unsigned python image not reported"

# Case 2: a digest the signer never signed and a listed image pinned by tag
# only are both policy failures (exit 1), each reported.
rc=0
err="$(run "$FIX/prov-drifted" 2>&1 >/dev/null)" || rc=$?
[ "$rc" -eq 1 ] || fail "prov-drifted: expected exit 1, got $rc"
grep -q "HTTP 404" <<<"$err" || fail "prov-drifted: unsigned digest not reported"
grep -q "not digest-pinned" <<<"$err" || fail "prov-drifted: tag-only listed image not reported"

# Case 3: a service outage means the check could not run (exit 2), which
# must stay distinguishable from a policy failure.
rc=0
STUB_DOWN=1 STUB_FAILURE="Error: HTTP 503: Service Unavailable" run "$FIX/prov-aligned" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 2 ] || fail "outage: expected exit 2, got $rc"

# Case 4: a repo with no Dockerfiles or compose files is skipped (exit 0).
out="$(run "$FIX/prov-absent")" || fail "prov-absent: expected exit 0"
grep -q "skipping" <<<"$out" || fail "prov-absent: expected a skip message, got: $out"

echo "provenance smoke OK"
