# Documentation

These documents complement the [top-level README](../README.md). The README
answers what this repo offers and how to call one workflow; everything here is
the reference material behind it — full input schemas, the rules consumers must
mirror, and the release ritual.

| Document | What it covers |
|---|---|
| [workflows.md](workflows.md) | Per-workflow reference: inputs, defaults, prerequisites and options for `python-app-ci`, `infra-validation`, `node-lib-ci`, `claude` and `release-tag`. |
| [pinning.md](pinning.md) | The two commit-SHA pinning policies — Actions `uses:` refs and the `@infra/ui` frontend dependency — and the validators that enforce them. |
| [strict-python.md](strict-python.md) | The canonical ruff/pyrefly/pre-commit config Python consumers must mirror, how the drift check works, and the intentional choices behind it. |
| [vendored-files.md](vendored-files.md) | The files copied byte-for-byte into consumers (`make/common.mk`, `bundle-lib.sh`, `eslint.config.js`) and the include-driven rule for when each is required. |
| [versioning.md](versioning.md) | How workflow versions are tagged and the two-step release that keeps the major alias current. |
| [maintaining.md](maintaining.md) | Working inside this repo: what self-CI checks, and running the validators locally. |

Design history — dated specs and implementation plans — lives alongside in
`superpowers/specs/` and `superpowers/plans/`; those are point-in-time records,
not current reference.

## Who this is for

- **Wiring a new consumer repo into shared CI** — start with the caller snippet
  in the [README](../README.md), then [workflows.md](workflows.md) for the
  inputs your repo needs and [pinning.md](pinning.md) for the `uses:` pin.
- **Adopting the shared Python config or build glue** —
  [strict-python.md](strict-python.md) and
  [vendored-files.md](vendored-files.md) say exactly what to mirror and what CI
  compares.
- **Cutting a release of this repo, or debugging a consumer's failing lint job**
  — [versioning.md](versioning.md) for the tag ritual,
  [maintaining.md](maintaining.md) for the self-CI contract.

## Conventions used in these docs

- Paths are relative to the repo root unless shown otherwise; `../chorus` style
  paths assume consumers are checked out beside this repo.
- `<commit-sha>` in a snippet is a placeholder for a full 40-character SHA — see
  [pinning.md](pinning.md#action-refs) for resolving one.
- The doubled `.github/.github/` in a `uses:` path is correct, not a typo.
