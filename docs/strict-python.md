# Strict-mode Python config

The `lint` job in `python-app-ci.yml` checks out **this repo at the same
ref the workflow is running at** and diffs the consumer's `pyproject.toml`
and `.pre-commit-config.yaml` against [`configs/python-strict/`](../configs/python-strict/).
Any drift fails CI.

The guarantee: a consumer pinned to tag `vN` is always validated against
the canonical config that shipped with `vN`.

Consumers must mirror, exactly:

1. **`[tool.ruff]` in `pyproject.toml`** ← [`ruff.toml`](../configs/python-strict/ruff.toml).
   The only key a consumer may override is `target-version` (each repo
   has a different Python floor).
2. **`[tool.pyrefly]` in `pyproject.toml`** ← [`pyrefly.toml`](../configs/python-strict/pyrefly.toml).
3. **`rev:` for the ruff and pyrefly hooks in `.pre-commit-config.yaml`**
   ← [`precommit-versions.yaml`](../configs/python-strict/precommit-versions.yaml).

To check alignment locally from a consumer repo:

```bash
python3 ../.github/scripts/validate_strict_config.py
```

(adjust the path; or pass `--consumer-root`). Exits 0 on alignment, 1 on
drift, with concrete entries on stderr.

A few intentional choices worth knowing:

- `ignore-missing-imports = ["*"]` in `pyrefly.toml` is load-bearing — without
  it, strict mode would fail on every untyped third-party import
  (Streamlit, the Neo4j driver, llama-index, etc.). Strict applies to
  first-party code; transitive untyped seams are out of scope.
- `ANN401` (forbid `Any`) is ignored in `ruff.toml` for the same reason:
  bridges to untyped libraries force `Any` constantly, and strict pyrefly
  is the actual rigor.
- The canonical regime is `preset = "strict"` — pyrefly's full strict checks.
  `uv run pyrefly init pyproject.toml --non-interactive` scaffolds a starting
  `[tool.pyrefly]` block but emits a laxer migration default, so set
  `preset = "strict"` and mirror the canonical values after scaffolding.
