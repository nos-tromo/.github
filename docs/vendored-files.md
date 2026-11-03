# Vendored shared files

Beyond the merged-in strict config, five files are **vendored verbatim** into
consumers and drift-checked by the reusable workflows (`python-app-ci` checks
all five; `infra-validation` all but `eslint.config.js`):

| Vendored file | Canonical source | Validator |
|---------------|------------------|-----------|
| `make/common.mk` | [`configs/make-common/`](../configs/make-common/) | `scripts/validate_make_common.py` |
| `scripts/bundle-lib.sh` | [`configs/bundle/`](../configs/bundle/) | `scripts/validate_bundle_lib.py` |
| `frontend/eslint.config.js` | [`configs/frontend-eslint/`](../configs/frontend-eslint/) | `scripts/validate_eslint_config.py` |
| `audit_event.py` (anywhere in the repo) | [`configs/audit/`](../configs/audit/) | `scripts/validate_audit_lib.py` |
| `audit_runtime.py` (anywhere in the repo) | [`configs/audit/`](../configs/audit/) | `scripts/validate_audit_lib.py` |

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
- `audit_event.py`, `audit_runtime.py` — every copy found is checked against
  the canonical file of the same name, wherever the package layout puts it
  (`<pkg>/utils/audit_event.py`, `<pkg>/audit_runtime.py`, …). Virtual
  environments, `node_modules`, caches and the hub checkout are not searched.
  There is no separate opt-in to miss: code importing a module fails without
  it. A repo may vendor `audit_event.py` alone (the audit store's verifier
  does); emitting apps vendor both.

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

### The audit runtime module

`configs/audit/audit_runtime.py` is what every emitting app must do the same way
around the event module:

- **Where the lines go.** `open_audit_stream()` reads `AUDIT_OUTPUT`: unset or
  `stdout` keeps the emitter's default; `container` writes to the container's
  main stdout (`/proc/1/fd/1`) through `ContainerAuditStream`, which takes one
  container-wide `flock` around every write. A command started with
  `docker compose exec` thereby lands in the long-running container's log —
  a one-off container can exit before the log collector discovers it, losing
  its lines without a trace — and lines longer than a pipe's atomic write size
  never interleave. A write that cannot get the lock in time fails, and the
  emitter reports that as an `AuditWriteError`.
- **The stated purpose.** `parse_purpose_header()` decodes the `X-Audit-Purpose`
  request header: the form-encoded serialization of up to two fields,
  `case_ref` and `reason` (what a browser's `URLSearchParams` produces), each at
  most 256 characters and free of control characters. `encode_purpose()` is its
  inverse, `purpose_from_env()` the command-line equivalent (`AUDIT_CASE_REF`,
  `AUDIT_REASON`), and `parse_required_actions()` reads `AUDIT_REQUIRE_PURPOSE`,
  the actions a service refuses (`428`) without a stated purpose.
- **Correlation and size.** `clean_request_id()` keeps only sane `X-Request-ID`
  values; `id_chunks()` splits an id list across events instead of letting the
  event module cut it.

It imports nothing from `audit_event.py`, so it works under any package layout;
the tests assert that its caps equal the event module's.

So vendored-and-opted-in drift-checks; **missing-but-opted-in fails**; and
missing-and-not-opted-in is skipped (a legitimately bespoke repo, e.g.
`data-plane`, `open-webui`). A repo that adopts later becomes subject to the
check automatically — there is no exemption list to maintain.
