# Pinning policy

Two things every consumer repo must pin to an immutable commit SHA: the
Actions refs its workflows call, and — for repos with a frontend — the
`@infra/ui` design-system dependency. Both are enforced by a validator that
runs in the shared CI workflows.

## Action refs

Every `uses:` reference in every federation repo must name a **full
40-character commit SHA**, with the version it corresponds to in a trailing
comment:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
```

A tag or branch ref resolves at run time, so whoever controls the tag controls
what executes in CI — and Actions tags are mutable by design. A commit SHA is
content-addressed and cannot be moved under a consumer. Dependabot understands
this convention: it reads the trailing comment and rewrites both the SHA and
the comment when it opens a bump PR, so pinning costs no maintenance. Each
consumer needs a `github-actions` entry in its `.github/dependabot.yml`, one
per directory containing workflows or composite actions (Dependabot does not
descend into `actions/*/action.yml` on its own).

Two forms are exempt, having no mutable remote ref to pin: a local action
(`./path`, whatever the run already checked out) and a container action pinned
by image digest (`docker://image@sha256:…`).

[`scripts/validate_action_pins.py`](../scripts/validate_action_pins.py) enforces
this. It scans the consumer's `.github/workflows/*.yml|*.yaml` plus any
`action.yml|action.yaml` at the root or under `actions/*/`, and fails with a
`file:line` for each unpinned ref. It runs in the `python-app-ci` lint job, the
`infra-validation` make-common job, and a dedicated `action-pins` job in
`node-lib-ci`; a repo with no `.github/workflows/` directory is skipped. Run it
locally the same way as the other validators:

```bash
python3 scripts/validate_action_pins.py --consumer-root ../chorus
```

To resolve a tag to its SHA:
`git ls-remote https://github.com/actions/checkout refs/tags/v7.0.1`.

## infra-ui tarball pins

A `frontend` consuming the shared design system must reference it as a
**commit-SHA-pinned codeload tarball URL**, not the `github:` shorthand:

```jsonc
// frontend/package.json
"@infra/ui": "https://codeload.github.com/nos-tromo/infra-ui/tar.gz/<commit-sha>"  // correct
"@infra/ui": "github:nos-tromo/infra-ui#v0.2.1"                                    // wrong — breaks CI
```

A human `pnpm install` resolves the `github:` form to that same public HTTPS
tarball, so it looks fine locally. But when Dependabot regenerates
`pnpm-lock.yaml` for *any* frontend bump, it rewrites the entry to a
`git@github.com:` SSH resolution, which then fails both jobs: `frontend`
SSH-clones with no key (`Permission denied (publickey)`) and `docker` has no
`git` in the `node:*-alpine` builder (`pnpm: not found: git`). The pinned
tarball leaves no `github:` shorthand to rewrite and installs over HTTPS with
no key or git binary. Bump it by swapping the commit SHA (`git rev-list -n1
<tag>` in `infra-ui`); the lockfile `version`/`resolution` are unchanged, so
the re-lock diff is just the one `specifier` line.

[`scripts/validate_infra_ui_pin.py`](../scripts/validate_infra_ui_pin.py)
enforces this. It checks the frontend's `package.json` and, when present, its
`pnpm-lock.yaml` — pnpm records no integrity hash for tarball URLs, so a stale
tag-form lockfile would keep resolving the tag even after the manifest was
fixed. A consumer with no frontend, or one that does not depend on `@infra/ui`,
is skipped. It runs in the `python-app-ci` lint job beside the action-pin
check, and locally the same way as the other validators:

```bash
python3 scripts/validate_infra_ui_pin.py --consumer-root ../chorus
```
