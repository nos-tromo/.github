# Vendored shared files

Beyond the merged-in strict config, three files are **vendored verbatim** into
consumers and drift-checked by the reusable workflows (`python-app-ci` checks
all three; `infra-validation` the first two):

| Vendored file | Canonical source | Validator |
|---------------|------------------|-----------|
| `make/common.mk` | [`configs/make-common/`](../configs/make-common/) | `scripts/validate_make_common.py` |
| `scripts/bundle-lib.sh` | [`configs/bundle/`](../configs/bundle/) | `scripts/validate_bundle_lib.py` |
| `frontend/eslint.config.js` | [`configs/frontend-eslint/`](../configs/frontend-eslint/) | `scripts/validate_eslint_config.py` |

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

So vendored-and-opted-in drift-checks; **missing-but-opted-in fails**; and
missing-and-not-opted-in is skipped (a legitimately bespoke repo, e.g.
`data-plane`, `open-webui`). A repo that adopts later becomes subject to the
check automatically — there is no exemption list to maintain.
