# mypy → pyrefly Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace mypy with pyrefly as the canonical strict type-checker across the nos-tromo Python federation, preserving the centralized drift-checked config + tag-pinning architecture.

**Architecture:** The `nos-tromo/.github` repo owns a canonical strict-config that five consumer repos mirror, enforced by `validate_strict_config.py` (run from the reusable `python-app-ci.yml` lint job at the consumer's pinned tag). This migration (1) builds a pyrefly regime in `.github` and ships it as a **new major tag `v3`** (leaving `v2`/mypy frozen), then (2) cuts each consumer over one PR at a time by swapping its in-repo config and bumping its workflow ref `@v2 → @v3`. Per-repo atomic; federation-incremental; nothing flag-days.

**Tech Stack:** pyrefly 1.1.1 (Rust type-checker, `py3-none` wheel), `uv`, pre-commit (local hooks), GitHub Actions reusable workflows, Python stdlib (`tomllib`) for the validator.

## Global Constraints

- **pyrefly version:** `1.1.1` (verbatim) — pinned in each consumer's `dependency-groups.dev` as `pyrefly==1.1.1` and recorded in `configs/python-strict/precommit-versions.yaml`.
- **Canonical regime = the `pyrefly init` output** (faithful mypy-strict translation): `preset = "legacy"`, `ignore-missing-imports = ["*"]`, and `[errors] redundant-cast = "warn"`. Ratcheting toward `strict` error-kinds is an explicit **out-of-scope follow-up** (Task 10), not part of this migration.
- **`ignore-missing-imports = ["*"]` is load-bearing** — successor to mypy's `ignore_missing_imports = true`; without it, untyped third-party libs poison first-party checking.
- **pyrefly runs as a `local` pre-commit hook** (`entry: uv run pyrefly check`), mirroring the existing local-mypy pattern, so it resolves types against the project venv.
- **Per-repo exclusions go in hook args** (`--project-excludes`), never in `[tool.pyrefly]` — the validator does an exact diff of `[tool.pyrefly]`, so any extra config key fails drift.
- **The `[tool.pyrefly]` block must match canonical exactly** across all five consumers (the only permitted cross-repo difference remains `[tool.ruff] target-version`, unchanged by this migration).
- **Python version floors are unchanged** and venvs are not interchangeable: chorus 3.12, docint 3.11, Nextext 3.12, translator 3.11, vllm-service 3.11.
- **CI uses `uv sync --frozen`** — every dev-dep change must commit an updated `uv.lock` in the same PR.
- **Two-step tag release** (from `.github/README.md` Versioning): cut the immutable tag, then move the major alias with `git tag -f` + `git push --force`. Forgetting step 2 strands consumers.

## Empirical baseline (measured 2026-06-22, pyrefly 1.1.1, `legacy` preset)

All five repos pass mypy clean today. pyrefly `legacy` deltas on that green code: **translator 0, vllm-service 0, Nextext 4, chorus 4, docint 28.** Speed: chorus 7.3s→0.3s, docint 36.4s→1.0s (mypy is cold every run — canonical sets `no_incremental = true`). Airgap wheel confirmed on PyPI: `pyrefly-1.1.1-py3-none-manylinux_2_17_x86_64.whl` (one `py3-none` wheel covers all Python versions). No pydantic-model or FastAPI `Depends()` false positives observed.

## File Structure

**Phase 1 — central infra (repo: `nos-tromo/.github`, current dir):**
- Create `configs/python-strict/pyrefly.toml` — canonical pyrefly regime (replaces `mypy.ini` as the type-check source of truth).
- Delete `configs/python-strict/mypy.ini`.
- Modify `configs/python-strict/precommit-versions.yaml` — swap `mypy` pin → `pyrefly` pin.
- Modify `scripts/validate_strict_config.py` — replace the mypy half of the diff with a pyrefly half (TOML-native; deletes the `configparser`/INI machinery).
- Modify `tests/fixtures/aligned/{pyproject.toml,.pre-commit-config.yaml}` and `tests/fixtures/drifted/{pyproject.toml,.pre-commit-config.yaml}` — the validator's tests.
- Modify `.github/workflows/python-app-ci.yml` (comments only) and `README.md`, `profile/README.md` (docs).

**Phase 2 — consumers (one PR each, in its own repo):**
- Per repo: `pyproject.toml` (`[tool.mypy]`→`[tool.pyrefly]`, dev pin), `.pre-commit-config.yaml` (mypy hook→pyrefly hook), `uv.lock` (re-locked), `.github/workflows/ci.yml` (`@v2`→`@v3`), plus any per-repo type fixes/suppressions.

---

## Task 1: Canonical pyrefly config + version pin (`.github` repo)

**Files:**
- Create: `configs/python-strict/pyrefly.toml`
- Delete: `configs/python-strict/mypy.ini`
- Modify: `configs/python-strict/precommit-versions.yaml`

**Interfaces:**
- Produces: the canonical regime `{preset="legacy", ignore-missing-imports=["*"], errors.redundant-cast="warn"}` and the version key `pyrefly: "1.1.1"` — consumed by the validator (Task 2) and mirrored by every consumer (Tasks 5–9).

- [ ] **Step 1: Create the canonical pyrefly config**

Create `configs/python-strict/pyrefly.toml`:

```toml
# Canonical pyrefly regime for nos-tromo Python apps (v3+).
#
# Mirror this into each consumer's pyproject.toml under [tool.pyrefly] (the
# [errors] table becomes [tool.pyrefly.errors]). The validate_strict_config.py
# check in python-app-ci.yml diffs the consumer's [tool.pyrefly] block against
# this file and fails CI on drift.
#
# This is exactly what `pyrefly init` emits when migrating the previous
# [tool.mypy] strict config, so a consumer can regenerate it mechanically:
#   uv run pyrefly init pyproject.toml --non-interactive
#
# preset = "legacy" is pyrefly's mypy-compatibility preset. ignore-missing-imports
# = ["*"] is load-bearing — the direct successor to mypy's
# ignore_missing_imports = true: it keeps untyped third-party libs (Streamlit,
# the Neo4j driver, llama-index, the OpenAI SDK, etc.) from poisoning
# first-party checking. Strict applies to code we control.
preset = "legacy"
ignore-missing-imports = ["*"]

[errors]
redundant-cast = "warn"
```

- [ ] **Step 2: Delete the old mypy config**

```bash
git rm configs/python-strict/mypy.ini
```

- [ ] **Step 3: Swap the version pin**

Replace the entire contents of `configs/python-strict/precommit-versions.yaml` with:

```yaml
# Canonical pre-commit hook versions for the strict regime.
#
# - ruff: pinned to a release rev of astral-sh/ruff-pre-commit; consumers
#   mirror the rev exactly.
# - pyrefly: NOT pinned to a pre-commit rev. Like mypy before it, pyrefly needs
#   to see every dep's types (FastAPI/pydantic/OpenAI-SDK decorators, base
#   classes, return types) and pre-commit's isolated env can't keep up with
#   each repo's dep set. The canonical pyrefly hook is a *local* hook that runs
#   `uv run pyrefly check` so it uses the project's venv. Pin the pyrefly
#   version in your dev deps (e.g. `pyrefly==1.1.1`) — the validator checks
#   that pin instead.

ruff: v0.15.14
pyrefly: "1.1.1"  # checked against pyproject dev deps, not a pre-commit rev
```

- [ ] **Step 4: Verify the config parses and pyrefly accepts it**

```bash
python3 -c "import tomllib; print(tomllib.loads(open('configs/python-strict/pyrefly.toml').read()))"
uvx pyrefly@1.1.1 check --help >/dev/null && echo "pyrefly reachable"
```
Expected: a dict with `preset`, `ignore-missing-imports`, `errors`; then `pyrefly reachable`.

- [ ] **Step 5: Commit**

```bash
git add configs/python-strict/pyrefly.toml configs/python-strict/precommit-versions.yaml
git commit -m "feat(python-strict): add canonical pyrefly regime, retire mypy.ini"
```

---

## Task 2: Rewrite the validator + fixtures (TDD) (`.github` repo)

The fixtures **are** the validator's tests (exercised by `self-ci.yml`'s `validator-smoke` job). Write the new fixtures first (red), then rewrite the validator (green).

**Files:**
- Modify: `tests/fixtures/aligned/pyproject.toml`, `tests/fixtures/aligned/.pre-commit-config.yaml`
- Modify: `tests/fixtures/drifted/pyproject.toml`, `tests/fixtures/drifted/.pre-commit-config.yaml`
- Modify: `scripts/validate_strict_config.py`

**Interfaces:**
- Consumes: canonical `pyrefly.toml` + `precommit-versions.yaml` (Task 1).
- Produces: a validator that passes a consumer iff its `[tool.pyrefly]` mirrors canonical, it pins `pyrefly==1.1.1` in `dependency-groups.dev`, and its `.pre-commit-config.yaml` contains a local `uv run pyrefly check` hook. This contract is what Tasks 5–9 must satisfy.

- [ ] **Step 1: Rewrite the `aligned` fixture (expected-PASS)**

In `tests/fixtures/aligned/pyproject.toml`, replace the `[tool.mypy]` block:

```toml
[tool.mypy]
strict = true
ignore_missing_imports = true
no_incremental = true
```

with:

```toml
[tool.pyrefly]
preset = "legacy"
ignore-missing-imports = ["*"]

[tool.pyrefly.errors]
redundant-cast = "warn"
```

and replace the dev-deps line `"mypy==1.19.0",` with `"pyrefly==1.1.1",`. Leave the `[tool.ruff]` blocks and the `target-version = "py312"` override untouched (they still exercise the allowed-override path).

In `tests/fixtures/aligned/.pre-commit-config.yaml`, replace the `- repo: local` mypy hook block:

```yaml
  - repo: local
    hooks:
      - id: mypy
        name: mypy
        entry: uv run mypy
        language: system
        types: [python]
        require_serial: true
        pass_filenames: false
        args: ["."]
```

with:

```yaml
  - repo: local
    hooks:
      - id: pyrefly
        name: pyrefly
        entry: uv run pyrefly check
        language: system
        types: [python]
        require_serial: true
        pass_filenames: false
```

Also update the file's header comment from "mypy is a `local` hook" to "pyrefly is a `local` hook".

- [ ] **Step 2: Rewrite the `drifted` fixture (expected-FAIL, ≥2 distinct drifts)**

Read `tests/fixtures/drifted/pyproject.toml` and `tests/fixtures/drifted/.pre-commit-config.yaml`. Apply the same mypy→pyrefly swap as Step 1 **but introduce two deliberate drifts** so the validator must reject it:

In `tests/fixtures/drifted/pyproject.toml`, add a `[tool.pyrefly]` that drifts on a value, and leave the dev pin stale:

```toml
[tool.pyrefly]
preset = "basic"
ignore-missing-imports = ["*"]

[tool.pyrefly.errors]
redundant-cast = "warn"
```

and set the dev-deps to keep the **old** pin (a second drift): `"mypy==1.19.0",` (do NOT add `pyrefly==1.1.1`).

In `tests/fixtures/drifted/.pre-commit-config.yaml`, leave it with the **old mypy local hook** (so the "must contain `uv run pyrefly check`" check also fails). If the existing drifted pre-commit already lacks a pyrefly hook, no change is needed there.

- [ ] **Step 3: Run the smoke test to confirm RED (validator still mypy-shaped)**

```bash
python3 scripts/validate_strict_config.py --consumer-root tests/fixtures/aligned; echo "aligned exit=$?"
```
Expected: **FAIL** (non-zero) — the current validator looks for `[tool.mypy]` / `uv run mypy` / `mypy==` and won't find them in the now-pyrefly aligned fixture. This proves the fixtures changed before the validator does.

- [ ] **Step 4: Rewrite the validator — imports, constants, loaders**

In `scripts/validate_strict_config.py`:

(a) Delete `import configparser` (line ~19).

(b) Delete the `MYPY_PRECOMMIT_REPO = ...` constant (line ~28). Keep `RUFF_PRECOMMIT_REPO`.

(c) Delete the `_coerce_ini_value(...)` function (whole def) and the `_load_canonical_mypy(...)` function (whole def).

(d) Add this loader next to `_load_canonical_ruff`:

```python
def _load_canonical_pyrefly(path: Path) -> dict[str, Any]:
    """Parse the canonical ``pyrefly.toml`` into flattened-key form.

    Args:
        path: Filesystem path to the canonical ``pyrefly.toml``.

    Returns:
        The TOML contents as a single-level dict keyed by dotted table
        paths (see :func:`_flatten`), e.g. ``errors.redundant-cast``.
    """
    return _flatten(tomllib.loads(path.read_text()))
```

(e) Update the module docstring: change "the consumer's pyproject.toml [tool.ruff] and [tool.mypy] blocks against configs/python-strict/ruff.toml and configs/python-strict/mypy.ini" to "... [tool.ruff] and [tool.pyrefly] blocks against configs/python-strict/ruff.toml and configs/python-strict/pyrefly.toml".

- [ ] **Step 5: Rewrite the validator — `main()` body**

In `main()`:

(a) Replace `canonical_mypy = args.canonical_dir / "mypy.ini"` with:
```python
    canonical_pyrefly = args.canonical_dir / "pyrefly.toml"
```

(b) In the `required` dict, replace `"canonical mypy.ini": canonical_mypy,` with:
```python
        "canonical pyrefly.toml": canonical_pyrefly,
```

(c) Replace `consumer_mypy = pyproject.get("tool", {}).get("mypy", {})` with:
```python
    consumer_pyrefly = _flatten(pyproject.get("tool", {}).get("pyrefly", {}))
```

(d) Replace the `[tool.mypy]` diff line with:
```python
    drifts += _diff("tool.pyrefly", consumer_pyrefly, _load_canonical_pyrefly(canonical_pyrefly), set())
```

(e) Replace the entire mypy pre-commit block (from `# mypy is a *local* pre-commit hook ...` through the `expected_pin = f"mypy=={expected_mypy}"` drift append) with:

```python
    # pyrefly is a *local* pre-commit hook running `uv run pyrefly check` so it
    # sees the project venv (resolving FastAPI/pydantic/etc. types needs the
    # installed deps). The pyrefly version itself is pinned in dev deps.
    expected_pyrefly = versions.get("pyrefly")
    if not expected_pyrefly:
        drifts.append("  [precommit-versions.yaml] canonical missing entry: pyrefly")
    else:
        if "uv run pyrefly check" not in precommit_text:
            drifts.append(
                "  [.pre-commit-config.yaml] pyrefly hook must be a `local` hook with "
                "`entry: uv run pyrefly check` (so pyrefly sees the project venv)"
            )
        dev_deps = (
            pyproject.get("dependency-groups", {}).get("dev", [])
            if isinstance(pyproject.get("dependency-groups"), dict)
            else []
        )
        expected_pin = f"pyrefly=={expected_pyrefly}"
        if expected_pin not in dev_deps:
            drifts.append(
                f"  [pyproject.toml dependency-groups.dev] must pin {expected_pin!r} "
                f"(found: {[d for d in dev_deps if isinstance(d, str) and d.startswith('pyrefly')]})"
            )
```

(f) Update the final fix-hint print so it reads:
```python
        print(
            "\nTo fix: mirror nos-tromo/.github/configs/python-strict/ contents into\n"
            "  - your pyproject.toml ([tool.ruff], [tool.pyrefly], dev pyrefly pin)\n"
            "  - your .pre-commit-config.yaml (ruff rev + local pyrefly hook running `uv run pyrefly check`)",
            file=sys.stderr,
        )
```

- [ ] **Step 6: Run the smoke test to confirm GREEN**

```bash
python3 scripts/validate_strict_config.py --consumer-root tests/fixtures/aligned; echo "aligned exit=$?"
python3 scripts/validate_strict_config.py --consumer-root tests/fixtures/drifted; echo "drifted exit=$?"
```
Expected: `aligned exit=0` then `drifted exit=1` (with concrete drift lines on stderr naming `tool.pyrefly` `preset` and the missing `pyrefly==1.1.1` pin).

- [ ] **Step 7: Lint the validator with the canonical ruff (self-CI parity)**

```bash
uvx ruff@0.15.14 check --config configs/python-strict/ruff.toml scripts/
uvx ruff@0.15.14 format --config configs/python-strict/ruff.toml --check scripts/
```
Expected: both clean (no findings). Fix any (e.g. unused `Any` import) and re-run.

- [ ] **Step 8: Commit**

```bash
git add scripts/validate_strict_config.py tests/fixtures/aligned tests/fixtures/drifted
git commit -m "feat(validators): switch strict-config check from mypy to pyrefly"
```

---

## Task 3: Update workflow comments + docs (`.github` repo)

**Files:**
- Modify: `.github/workflows/python-app-ci.yml` (comments only)
- Modify: `README.md`
- Modify: `profile/README.md`

- [ ] **Step 1: Update `python-app-ci.yml` comments**

The workflow logic is unchanged (it runs the validator + `uv run pre-commit run --all-files`, which now invokes pyrefly). Update only the human-facing strings: change the header comment "Runs pre-commit (ruff-check + ruff-format + mypy ...)" to "... + pyrefly ...", and the `run-tests` input description mention of "ruff + mypy lint job" to "ruff + pyrefly lint job".

- [ ] **Step 2: Update `README.md`**

Replace mypy references with pyrefly throughout the "Strict-mode Python config" and "Using the Python-app workflow" sections: the bullet list item "`[tool.mypy]` in `pyproject.toml` ← `mypy.ini`" becomes "`[tool.pyrefly]` in `pyproject.toml` ← `pyrefly.toml`"; the "ruff + mypy via the consumer's own `.pre-commit-config.yaml`" line becomes "ruff + pyrefly ..."; and the `ignore_missing_imports = true` note becomes the `ignore-missing-imports = ["*"]` note (same load-bearing rationale). Add one sentence noting the regime is reproducible via `uv run pyrefly init pyproject.toml --non-interactive`.

- [ ] **Step 3: Update `profile/README.md`**

Replace any mypy mention with the pyrefly equivalent (grep first: `git grep -n mypy profile/README.md`).

- [ ] **Step 4: Verify no stray mypy references remain in shipped assets**

```bash
git grep -n -i mypy -- configs/ scripts/ README.md profile/ .github/workflows/python-app-ci.yml
```
Expected: no output (empty). Any hit is a missed reference — fix it.

- [ ] **Step 5: Commit**

```bash
git add README.md profile/README.md .github/workflows/python-app-ci.yml
git commit -m "docs: document the pyrefly strict regime (was mypy)"
```

---

## Task 4: Release v3.0 and move the `v3` alias (`.github` repo)

**Interfaces:**
- Consumes: Tasks 1–3 merged to `main`.
- Produces: tags `v3.0` and `v3` pointing at the pyrefly-regime commit. `v2`/`v2.x` remain frozen on the mypy regime. Tasks 5–9 pin `@v3`.

- [ ] **Step 1: Open the PR and confirm self-CI is green**

Push the Task 1–3 commits on a branch and open a PR. Confirm the `self-ci.yml` jobs pass — specifically `validator-smoke` (aligned→0, drifted→1) and `lint-validator` (ruff on `scripts/`).

```bash
gh pr create --fill --base main
gh pr checks --watch
```
Expected: all checks pass.

- [ ] **Step 2: Merge**

```bash
gh pr merge --squash --delete-branch
git checkout main && git pull
```

- [ ] **Step 3: Cut the immutable `v3.0` tag**

```bash
git tag -a v3.0 -m "v3.0: pyrefly replaces mypy as the canonical strict type-checker"
git push origin v3.0
```

- [ ] **Step 4: Move the `v3` major alias (the easy-to-forget second step)**

```bash
git tag -f -a v3 -m "v3: pyrefly strict regime"
git push origin v3 --force
```

- [ ] **Step 5: Verify both tags resolve to the same commit**

```bash
git rev-parse v3.0 v3
```
Expected: two identical SHAs. `v2` is intentionally left untouched (frozen mypy regime for not-yet-migrated consumers).

---

## Task 5: Cut over `translator` (repo: `translator`) — clean template (0 errors)

translator measured **0 pyrefly errors** at `legacy`, so this is the reference cutover with no triage. Run it in `../translator`.

**Files:**
- Modify: `pyproject.toml`, `.pre-commit-config.yaml`, `uv.lock`, `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the `@v3` validator contract (Task 2) and `v3` tag (Task 4).

- [ ] **Step 1: Swap the type-checker config in `pyproject.toml`**

Replace the `[tool.mypy]` block:
```toml
[tool.mypy]
# Mirrors nos-tromo/.github/configs/python-strict/mypy.ini. Drift fails CI.
strict = true
ignore_missing_imports = true
no_incremental = true
```
with:
```toml
[tool.pyrefly]
# Mirrors nos-tromo/.github/configs/python-strict/pyrefly.toml. Drift fails CI.
preset = "legacy"
ignore-missing-imports = ["*"]

[tool.pyrefly.errors]
redundant-cast = "warn"
```
and in `dependency-groups.dev`, replace `"mypy==1.19.0",` with `"pyrefly==1.1.1",`.

- [ ] **Step 2: Swap the pre-commit hook in `.pre-commit-config.yaml`**

Replace the local mypy hook block with:
```yaml
  # pyrefly is a local hook so it uses the project venv — resolving every dep's
  # types (FastAPI, the OpenAI SDK, etc.) needs the installed deps; pre-commit's
  # isolated env can't keep up. Pinned to pyrefly==<canonical> in dev deps;
  # canonical at nos-tromo/.github/configs/python-strict/precommit-versions.yaml.
  - repo: local
    hooks:
      - id: pyrefly
        name: pyrefly
        entry: uv run pyrefly check
        language: system
        types: [python]
        require_serial: true
        pass_filenames: false
```

- [ ] **Step 3: Re-lock and sync**

```bash
uv lock
uv sync --frozen --group dev
```
Expected: `uv.lock` now contains pyrefly 1.1.1 and not mypy; venv has `pyrefly`.

- [ ] **Step 4: Run pyrefly — confirm clean**

```bash
uv run pyrefly check; echo "exit=$?"
```
Expected: `0 errors` and `exit=0`. (translator measured clean; if anything appears, classify each finding — fix a real type issue at the source, else suppress on the reported line with `# pyrefly: ignore[<error-kind>]  # <reason>` — and re-run until clean.)

- [ ] **Step 5: Bump the workflow ref to `@v3`**

In `.github/workflows/ci.yml`, change `uses: nos-tromo/.github/.github/workflows/python-app-ci.yml@v2` to `@v3`.

- [ ] **Step 6: Run the full pre-commit suite and the drift validator locally**

```bash
uv run pre-commit run --all-files
python3 ../.github/scripts/validate_strict_config.py --consumer-root .; echo "drift exit=$?"
```
Expected: pre-commit passes (ruff + pyrefly); `drift exit=0`. (The local validator path assumes the `.github` repo is checked out as a sibling; if not, run against the `v3` checkout.)

- [ ] **Step 7: Commit and open the PR**

```bash
git checkout -b chore/migrate-mypy-to-pyrefly
git add pyproject.toml .pre-commit-config.yaml uv.lock .github/workflows/ci.yml
git commit -m "chore: migrate type-checking from mypy to pyrefly (@v3)"
gh pr create --fill --base main
gh pr checks --watch
```
Expected: the `lint` job (validates against `@v3` + runs pyrefly) and `test` matrix pass.

---

## Task 6: Cut over `vllm-service` (repo: `vllm-service`) — lint-only (0 errors)

vllm-service measured **0 errors**. It calls `python-app-ci.yml` with `run-tests: false` **and** `infra-validation.yml`; only the former changes.

**Files:**
- Modify: `pyproject.toml`, `.pre-commit-config.yaml`, `uv.lock`, `.github/workflows/ci.yml`

- [ ] **Step 1: Swap config + dev pin in `pyproject.toml`**

Apply the same `[tool.mypy]`→`[tool.pyrefly]` block replacement and dev-pin swap (`mypy==1.19.0`→`pyrefly==1.1.1`) as Task 5 Step 1. (vllm-service's `[tool.mypy]` has no header comment; the new block keeps the canonical mirror comment.)

- [ ] **Step 2: Swap the pre-commit hook**

vllm-service's mypy hook checks `src` (`args: ["src"]`). Replace the whole local hook with the canonical pyrefly hook **plus** an explicit include so it checks only `src/` (matching today's scope):
```yaml
  - repo: local
    hooks:
      - id: pyrefly
        name: pyrefly
        entry: uv run pyrefly check
        language: system
        types: [python]
        require_serial: true
        pass_filenames: false
        args: ["src"]
```
Note: passing `src` puts pyrefly in single-file/path mode over `src/`. Confirm in Step 4 that this resolves imports against the venv (it does — `uv run` provides the interpreter).

- [ ] **Step 3: Re-lock, sync, run pyrefly**

```bash
uv lock && uv sync --frozen --group dev
uv run pyrefly check src; echo "exit=$?"
```
Expected: `0 errors`, `exit=0`.

- [ ] **Step 4: Bump the `python-app-ci` ref only**

In `.github/workflows/ci.yml`, change the `python-lint` job's `uses: ...python-app-ci.yml@v2` to `@v3`. **Leave the `validate` job's `infra-validation.yml@v2` untouched** (unaffected by this migration).

- [ ] **Step 5: Validate locally, commit, PR**

```bash
uv run pre-commit run --all-files
python3 ../.github/scripts/validate_strict_config.py --consumer-root .; echo "drift exit=$?"
git checkout -b chore/migrate-mypy-to-pyrefly
git add pyproject.toml .pre-commit-config.yaml uv.lock .github/workflows/ci.yml
git commit -m "chore: migrate type-checking from mypy to pyrefly (@v3)"
gh pr create --fill --base main && gh pr checks --watch
```
Expected: `drift exit=0`; CI `python-lint` + `validate` both green.

---

## Task 7: Cut over `Nextext` (repo: `Nextext`) — 4 errors + build/legacy excludes

Nextext measured **4 errors** (`bad-argument-type`×3, `unsupported-delete`×1) and its mypy hook excludes `build/` and `legacy/`.

**Files:**
- Modify: `pyproject.toml`, `.pre-commit-config.yaml`, `uv.lock`, `.github/workflows/ci.yml`, plus any source files needing a fix/suppression.

- [ ] **Step 1: Swap config + dev pin** — same as Task 5 Step 1.

- [ ] **Step 2: Determine the exclude mechanism**

```bash
git check-ignore build legacy && echo "gitignored: pyrefly auto-skips" || echo "tracked: need explicit excludes"
uvx pyrefly@1.1.1 check --help | grep -A2 project-excludes
```
If both dirs are gitignored, pyrefly skips them automatically (it honors `.gitignore`) — no hook args needed. If tracked, note the exact `--project-excludes` flag syntax from the help for Step 3.

- [ ] **Step 3: Swap the pre-commit hook**

Use the canonical pyrefly hook (Task 5 Step 2). **If** Step 2 showed `build`/`legacy` are tracked, append the verified excludes, e.g.:
```yaml
        args: ["--project-excludes", "**/build/**", "--project-excludes", "**/legacy/**"]
```
(Excludes live in hook args, never in `[tool.pyrefly]`, to keep the canonical block exact.)

- [ ] **Step 4: Re-lock, sync, run pyrefly, triage the 4 findings**

```bash
uv lock && uv sync --frozen --group dev
uv run pyrefly check 2>&1 | tee /tmp/nextext-pyrefly.txt; echo "exit=$?"
```
For each of the 4 findings, classify and act, then re-run until `0 errors`:
1. **Real type issue mypy missed** → fix at the source (tighten the annotation / narrow the value).
2. **pyrefly stricter on a third-party callable/stub** → suppress on the reported line: `# pyrefly: ignore[<error-kind>]  # <reason>`.
3. **A pre-existing mypy `# type: ignore[...]` pyrefly relocates** → move it to pyrefly's reported line as `# pyrefly: ignore[<kind>]`.

Prefer real fixes over suppression; keep a one-line reason on every `# pyrefly: ignore`.

- [ ] **Step 5: Bump the workflow ref, validate, commit, and open the PR**

In `.github/workflows/ci.yml`, change `python-app-ci.yml@v2` → `@v3`. Then:
```bash
uv run pre-commit run --all-files
python3 ../.github/scripts/validate_strict_config.py --consumer-root .; echo "drift exit=$?"
git checkout -b chore/migrate-mypy-to-pyrefly
git add pyproject.toml .pre-commit-config.yaml uv.lock .github/workflows/ci.yml $(git diff --name-only)
git commit -m "chore: migrate type-checking from mypy to pyrefly (@v3)"
gh pr create --fill --base main && gh pr checks --watch
```
Expected: `drift exit=0`; CI `lint` + `test` jobs green.

---

## Task 8: Cut over `chorus` (repo: `chorus`) — 4 errors + airgap wheelhouse check

chorus measured **4 errors** at `legacy` (including the OpenAI-SDK overload that already carried a now-mislocated `# type: ignore[arg-type]`). chorus also vendors a `uv` wheelhouse for airgap — verify whether the type-checker belongs in it.

**Files:**
- Modify: `pyproject.toml`, `.pre-commit-config.yaml`, `uv.lock`, `.github/workflows/ci.yml`, source files for the 4 fixes/suppressions, and possibly the airgap bundle inputs.

- [ ] **Step 1: Swap config + dev pin** — same as Task 5 Step 1.

- [ ] **Step 2: Swap the pre-commit hook** — canonical pyrefly hook (Task 5 Step 2). chorus's mypy hook used `args: ["."]`; the pyrefly hook needs no path args (project mode).

- [ ] **Step 3: Re-lock, sync, run pyrefly, triage the 4 findings**

```bash
uv lock && uv sync --frozen --group dev
uv run pyrefly check 2>&1 | tee /tmp/chorus-pyrefly.txt; echo "exit=$?"
```
For each finding, classify and act (then re-run until `0 errors`): **(1)** real type issue → fix at source; **(2)** third-party-stub over-strictness → `# pyrefly: ignore[<kind>]  # <reason>` on the reported line; **(3)** a relocated pre-existing mypy ignore → move it to pyrefly's line. The two known cases here:
- `chorus/inference/provider.py` ~line 87/132 — `no-matching-overload` on `client.chat.completions.create(...)`. The existing `# type: ignore[arg-type]` sits on the `messages=` line but pyrefly reports at the call-start line. **Move/replace** the suppression: put `# pyrefly: ignore[no-matching-overload]  # OpenAI SDK rejects plain-dict messages; runtime-correct` on the `.create(` line (and drop the stale mypy ignore if pyrefly now flags it unused).
- `chorus/agent/loop.py` ~line 242/243 — `bad-argument-type` passing `object | None` as `result_count`. Prefer a real fix: narrow the type at the source (annotate/cast `result_count` to `int | None`) rather than suppress.
Re-run until `0 errors`.

- [ ] **Step 4: Airgap wheelhouse check**

```bash
git grep -nE "wheelhouse|uv (export|pip download|sync)|--no-dev|--group dev" -- Makefile make/ scripts/
```
Determine whether the airgap bundle vendors **dev** dependencies. The type-checker runs only in CI/dev (online), not in the runtime image, so the wheelhouse almost certainly builds with runtime deps only (`--no-dev`) — in which case **no action**. If, and only if, dev deps are vendored, add the pyrefly linux wheel to the wheelhouse:
```bash
uv pip download pyrefly==1.1.1 --python-platform x86_64-manylinux2014 --only-binary=:all: -d <wheelhouse-dir>
# vendors pyrefly-1.1.1-py3-none-manylinux_2_17_x86_64.whl (covers all Python versions)
```
Record the decision in the PR description either way.

- [ ] **Step 5: Bump the workflow ref, validate, commit, and open the PR**

In `.github/workflows/ci.yml`, change `python-app-ci.yml@v2` → `@v3`. Then (include any wheelhouse files touched in Step 4):
```bash
uv run pre-commit run --all-files
python3 ../.github/scripts/validate_strict_config.py --consumer-root .; echo "drift exit=$?"
git checkout -b chore/migrate-mypy-to-pyrefly
git add pyproject.toml .pre-commit-config.yaml uv.lock .github/workflows/ci.yml $(git diff --name-only)
git commit -m "chore: migrate type-checking from mypy to pyrefly (@v3)"
gh pr create --fill --base main && gh pr checks --watch
```
Expected: `drift exit=0`; CI `lint` + `test` jobs green.

---

## Task 9: Cut over `docint` (repo: `docint`) — 28 errors (heaviest triage)

docint measured **28 errors** at `legacy` (`bad-argument-type`×19, `not-iterable`×3, `missing-attribute`×3, `not-async`×1, `bad-return`×1, `bad-assignment`×1). This is the only task with substantial triage. Note: docint may have unrelated in-progress work on its branch — rebase onto a clean `main` first.

**Files:**
- Modify: `pyproject.toml`, `.pre-commit-config.yaml`, `uv.lock`, `.github/workflows/ci.yml`, and ~10–20 source files for fixes/suppressions.

- [ ] **Step 1–2: Swap config + dev pin + pre-commit hook** — same as Task 5 Steps 1–2.

- [ ] **Step 3: Re-lock, sync, capture the full finding list**

```bash
uv lock && uv sync --frozen --group dev
uv run pyrefly check 2>&1 | tee /tmp/docint-pyrefly.txt
grep -oE '\[[a-z0-9-]+\]$' /tmp/docint-pyrefly.txt | sort | uniq -c | sort -rn
```

- [ ] **Step 4: Triage procedure — apply to every finding until `0 errors`**

For each finding, classify and act:
1. **Real bug / too-loose type mypy missed** → fix at the source. Examples seen in docint: `int(row.get("_match_rank", 99))` where the value union includes `list` (narrow with `int(... or 0)` or assert the type); `expanded, debug_payload = expand_with_debug(query)` unpacking a value typed `object` (tighten `expand_with_debug`'s return annotation to the real tuple type); `bad-return` on `core/api.py` (widen the function's declared return type to match the dict it actually returns, or narrow the dict).
2. **pyrefly stricter on a third-party callable** (e.g. `anyio.to_thread.run_sync` ParamSpec) → suppress on the reported line: `# pyrefly: ignore[bad-argument-type]  # anyio run_sync ParamSpec over-strict`.
3. **Pre-existing mypy `# type: ignore[...]` that pyrefly relocates** → move the suppression to pyrefly's reported line and switch to `# pyrefly: ignore[<kind>]`.

Re-run `uv run pyrefly check` after each batch. Target: `0 errors`, `exit=0`. Prefer real fixes (category 1) over suppression; keep a one-line reason on every `# pyrefly: ignore`.

- [ ] **Step 5: Confirm tests still pass after the source edits**

```bash
uv run --no-sync pytest -q
```
Expected: green (the fixes are type-level; behavior unchanged). Investigate any failure before proceeding.

- [ ] **Step 6: Bump the workflow ref, validate, commit, and open the PR**

In `.github/workflows/ci.yml`, change `python-app-ci.yml@v2` → `@v3`. Then:
```bash
uv run pre-commit run --all-files
python3 ../.github/scripts/validate_strict_config.py --consumer-root .; echo "drift exit=$?"
git checkout -b chore/migrate-mypy-to-pyrefly
git add pyproject.toml .pre-commit-config.yaml uv.lock .github/workflows/ci.yml $(git diff --name-only)
git commit -m "chore: migrate type-checking from mypy to pyrefly (@v3)"
gh pr create --fill --base main && gh pr checks --watch
```
Expected: `drift exit=0`; CI `lint` + `test` jobs green. In the PR body, summarize how many of the 28 findings were real fixes vs suppressions (useful signal for the ratchet decision in Task 10).

---

## Task 10: Follow-ups (documented; schedule separately, not part of core cutover)

These are deliberately **out of scope** for the migration above but should be tracked.

- [ ] **Ratchet toward "strict":** once all five consumers are green on `legacy`, raise rigor incrementally by enabling pyrefly error-kinds in the canonical `[errors]` table (e.g. `missing-override-decorator`, `implicit-any`) one at a time, each shipped as a `v3.x` minor with the consumers' mirrored updates landing together (same atomic-release rule as today). Strict-preset deltas measured 2026-06-22: chorus 12, docint 135 (dominated by mechanical `@override` and empty-container annotations). Re-run the `pyrefly init`-vs-`strict` comparison before committing to a target.
- [ ] **Doc sweep:** remove remaining mypy mentions in each consumer's `CLAUDE.md` / `README.md` / `Makefile` comments (counts on 2026-06-22: chorus 11, translator 5, docint 4, Nextext 4, vllm-service 3), and the federation `infra/CLAUDE.md` "Conventions shared across the Python apps" section (which names mypy). These are doc-only and can be a single batch PR per repo.
- [ ] **Optional CI annotations:** consider `pyrefly check --output-format=github` (or the official `facebook/pyrefly` action) so findings annotate PRs inline. This is additive to the local hook.
- [ ] **Retire `v2`:** once all five consumers are confirmed on `@v3`, mark the `v2` line deprecated in `README.md` (do not delete the tags — they remain valid frozen references).

---

## Rollback

Each consumer PR is independently revertable, and `v2` (mypy regime) stays frozen and functional throughout. If a consumer's `@v3` cutover misbehaves: revert that repo's PR (restores `[tool.mypy]`, the mypy hook, the mypy dev pin, and `@v2`) — no central change needed. If the central `v3` regime itself is wrong, fix forward on `.github` and cut `v3.1`; consumers on `@v3` pick it up, consumers still on `@v2` are unaffected.

## Self-Review notes

- **Spec coverage:** validator rewrite (Task 2), canonical config (Task 1), fixtures (Task 2), per-repo rollout (Tasks 5–9), versioning/tags (Task 4), docs (Task 3), airgap (Task 8 Step 4) — all covered. All five mypy-gated repos (chorus, docint, Nextext, translator, vllm-service) have a task; data-plane/open-webui/deploy correctly excluded (no Python).
- **Type/name consistency:** the canonical keys `preset` / `ignore-missing-imports` / `errors.redundant-cast`, the dev pin `pyrefly==1.1.1`, the hook entry `uv run pyrefly check`, and the version key `pyrefly` in `precommit-versions.yaml` are identical across Task 1 (define), Task 2 (validate), and Tasks 5–9 (mirror).
- **Known runtime-discovered content:** the exact per-finding fixes in Tasks 7–9 are discovered by running pyrefly; the triage procedure (Task 9 Step 4) with measured examples is the specification, since the finding set is empirically known (4/4/28) but line-exact fixes depend on current source.
