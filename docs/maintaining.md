# Maintaining this repo

A self-CI workflow ([`.github/workflows/self-ci.yml`](../.github/workflows/self-ci.yml))
runs on every PR and push to `main`. It does two things:

1. **Lints `scripts/`** with `ruff check` and `ruff format --check`
   using the canonical strict config and the same ruff version every
   consumer gets (pinned in
   [`precommit-versions.yaml`](../configs/python-strict/precommit-versions.yaml)).
   The validator that enforces strict mode must itself pass strict mode.
2. **Smoke-tests the validators** against fixtures in
   [`tests/fixtures/`](../tests/fixtures/): an `aligned` fixture (must
   return 0; also exercises the `target-version` allowed-override and the
   `[tool.pyrefly]` mirror path) and a `drifted` fixture (must return non-zero),
   and the equivalent pairs for the vendored-file and action-pin validators.
   The `pins-smoke` job additionally runs `validate_action_pins.py` against
   this repo itself, so the hub is held to the policy it ships.

When anything under `configs/python-strict/` changes, the aligned
fixture must be updated to mirror it — same drift signal real
consumers get, applied to this repo's own fixture.

To run the validator against a real consumer locally:

```bash
python3 scripts/validate_strict_config.py --consumer-root ../chorus
```

