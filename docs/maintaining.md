# Maintaining this repo

A self-CI workflow ([`.github/workflows/self-ci.yml`](../.github/workflows/self-ci.yml))
runs on every PR and push to `main`, in nine jobs that do four things:

1. **Lints `scripts/`** (`lint-validator`) with `ruff check` and
   `ruff format --check` using the canonical strict config and the same ruff
   version every consumer gets (pinned in
   [`precommit-versions.yaml`](../configs/python-strict/precommit-versions.yaml)).
   The validator that enforces strict mode must itself pass strict mode.
2. **Smoke-tests every validator** against fixtures in
   [`tests/fixtures/`](../tests/fixtures/) — one job each, and each runs its
   validator against an `*-aligned` fixture (must return 0) and a `*-drifted`
   one (must return non-zero), plus the validator's own edge cases:

   | Job | Validator | Extra cases |
   |---|---|---|
   | `validator-smoke` | `validate_strict_config.py` | `half-migrated` (mypy left behind) must fail; `aligned` also exercises the `target-version` allowed-override and the `[tool.pyrefly]` mirror path |
   | `make-common-smoke` | `validate_make_common.py` | `mk-absent` skips, `mk-required-absent` fails; plus `tests/build_persist_smoke.sh` |
   | `bundle-lib-smoke` | `validate_bundle_lib.py` | `bundle-absent` skips, `bundle-required-absent` fails; plus `tests/bundle_version_smoke.sh` and `tests/bundle_checkout_smoke.sh` |
   | `eslint-config-smoke` | `validate_eslint_config.py` | `eslint-absent` skips |
   | `pins-smoke` | `validate_action_pins.py` | `pins-absent` skips; **and this repo itself**, so the hub is held to the policy it ships |
   | `uipin-smoke` | `validate_infra_ui_pin.py` | `uipin-absent` skips |
   | `profile-smoke` | `validate_profile_claims.py` | `profile-uncovered` (a row with no claim) must fail; `profile-member-absent` must exit **2**, not 1; **and this repo's own page**, via `--only-present` |

3. **Checks the public profile page** (`profile-smoke`): the fixture exit
   codes above, plus this repo's own `profile/README.md` against
   [`profile/claims.toml`](../profile/claims.toml) with `--only-present`, and
   the validator's unit tests. Only the hub-internal half runs here — page
   structure, quote coverage and the claims about this repository. The
   cross-repo half needs the member checkouts and runs weekly in
   [`profile-audit.yml`](../.github/workflows/profile-audit.yml).

   Two notes on that scheduled workflow. Its findings go to one rolling issue
   labelled `profile-drift`, reopened and rewritten rather than duplicated. And
   GitHub disables scheduled workflows after 60 days of repository inactivity,
   which only matters during a long absence; re-enable it by hand rather than
   adding keepalive commits.

4. **Unit-tests the release-tag action** (`release-tag-unit`): the pytest suite
   for [`actions/release-tag/extract_version.py`](../actions/release-tag/) — the
   version extractor and anti-downgrade comparator. Nothing else exercises them.

When you add or change a validator, add its smoke job here too.

When anything under `configs/python-strict/` changes, the aligned
fixture must be updated to mirror it — same drift signal real
consumers get, applied to this repo's own fixture. The same holds for
`tests/fixtures/{mk,bundle,eslint}-aligned/` and their canonical sources.

To run the validator against a real consumer locally:

```bash
python3 scripts/validate_strict_config.py --consumer-root ../chorus
```

