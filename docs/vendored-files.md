# Vendored shared files

Beyond the merged-in strict config, four files are **vendored verbatim** into
consumers and drift-checked by the reusable workflows (`python-app-ci` checks
all four; `infra-validation` all but `eslint.config.js`):

| Vendored file | Canonical source | Validator |
|---------------|------------------|-----------|
| `make/common.mk` | [`configs/make-common/`](../configs/make-common/) | `scripts/validate_make_common.py` |
| `scripts/bundle-lib.sh` | [`configs/bundle/`](../configs/bundle/) | `scripts/validate_bundle_lib.py` |
| `frontend/eslint.config.js` | [`configs/frontend-eslint/`](../configs/frontend-eslint/) | `scripts/validate_eslint_config.py` |
| `audit_event.py` (anywhere in the repo) | [`configs/audit/`](../configs/audit/) | `scripts/validate_audit_lib.py` |

`common.mk` also carries the two local gates that mirror `python-app-ci`:
`make verify` (pre-commit + frontend eslint/build) and `make test` (pytest +
frontend vitest; `make test-backend` / `make test-frontend` run one half —
the frontend half is skipped when there is no `frontend/`, the backend half
when the repo sets `TESTS := no`).

Unlike the strict config (merged into `pyproject.toml` and compared
semantically), these are copied byte-for-byte — the check is an exact file
comparison, so re-vendor on change rather than hand-editing the copy.

**Required-ness is include-driven** — a vendored file is enforced only where the
repo opts in, so a bespoke repo is never forced to adopt:

- `make/common.mk` — required iff the `Makefile` has `include make/common.mk`.
- `scripts/bundle-lib.sh` — required iff `scripts/bundle_images.sh` sources it.
- `frontend/eslint.config.js` — checked only when present (frontends are optional).
- `audit_event.py` — every copy found is checked, wherever the package layout
  puts it (`<pkg>/utils/audit_event.py`, `<pkg>/audit_event.py`, …). Virtual
  environments, `node_modules`, caches and the hub checkout are not searched.
  There is no separate opt-in to miss: code importing the module fails without
  it.

### The audit-event module

`configs/audit/audit_event.py` writes and verifies the federation's audit
stream: one JSON line per event on stdout, marked `"stream": "audit"` and
hash-chained per process (`seq`, `prev_hash`, `hash` over the event's
canonical JSON), with `start`, periodic `heartbeat` and `shutdown` lifecycle
events. `verify_chain()` reports altered events, broken links, gaps, repeats
and chains that start mid-way, and returns per-chain heads to verify the next
window from. Emitters and verifiers must agree byte-for-byte on canonical JSON
and hashing, which is why this one is vendored rather than reimplemented. The
field contract is [`configs/audit/audit-event.schema.json`](../configs/audit/audit-event.schema.json).
Stdlib only, Python 3.11+, and clean under the strict ruff + pyrefly regime the
apps run on every vendored copy.

So vendored-and-opted-in drift-checks; **missing-but-opted-in fails**; and
missing-and-not-opted-in is skipped (a legitimately bespoke repo, e.g.
`data-plane`, `open-webui`). A repo that adopts later becomes subject to the
check automatically — there is no exemption list to maintain.
