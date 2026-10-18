# Pinning policy

Two things every consumer repo must pin to an immutable commit SHA: the
Actions refs its workflows call, and — for repos with a frontend — the
`@infra/ui` design-system dependency. Both are enforced by a validator that
runs in the shared CI workflows. A third check covers the container images
consumers pin by digest: where the publisher signs its images, the pinned
digest must verify against that publisher
([Signed image provenance](#signed-image-provenance)).

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

## Signed image provenance

A digest pin (`image:tag@sha256:…`) makes an image immutable, but it says
nothing about who built it: whoever controls the tag chose the digest, and a
Dependabot PR that moves a pin proposes whatever the tag points to now. For
publishers that sign their images, the signature closes that gap.

[`scripts/validate_image_provenance.py`](../scripts/validate_image_provenance.py)
scans a consumer's Dockerfiles (`FROM`, `COPY --from=`, with `ARG` defaults
expanded) and compose files (`image:`, with `${VAR:-default}` expanded). It
checks every ref against the signer policy in
[`configs/image-provenance/signers.toml`](../configs/image-provenance/signers.toml),
which the script reads in place from the pinned hub revision; consumers vendor
nothing. Each entry names an image and how its publisher signs:

| Method | Verifier | Used for |
|---|---|---|
| `github-attestation` | `gh attestation verify`, pinned to the publisher's repo, source ref and GitHub-hosted runners | `ghcr.io/astral-sh/uv` |
| `cosign-key` | `cosign verify --key` with the publisher's public key, pinned in `configs/image-provenance/keys/` | LiteLLM, the NVIDIA DCGM exporter |
| `cosign-keyless` | `cosign verify` against a Sigstore certificate identity and OIDC issuer | Qdrant |

The rules:

- Every digest-pinned ref to a listed image must verify, or the check fails.
  This matters most on a Dependabot PR that moves a pin, because the new
  digest is verified before anyone merges it.
- A listed image pinned by tag only fails: a signature covers a digest, so
  there is nothing to verify without one.
- Images not on the list are reported in the job summary as unsigned. Most
  upstream images publish no signature at all, including every Docker
  Official Image (`python`, `node`, `nginx`, `debian`, …).
- The check fails closed around listed images it cannot read. A listed name
  on any non-comment line that was not parsed as a reference (a YAML block
  scalar or flow mapping, an `ARG` used outside `FROM`) is a failure, and so
  is a ref whose unresolved variable could stand for a listed image. Other
  refs with unresolved variables are skipped with a warning.
- Exit 0 means every listed image verified, 1 is a policy failure, and 2
  means the check could not run (a verifier missing, a network or service
  error). Both non-zero codes fail CI; 2 only labels the cause.

It runs in the `python-app-ci` lint job and the `infra-validation`
make-common job, which install cosign first. `gh` reads attestations with the
job's own `github.token`, so callers need no extra permissions. To run it
locally you need an authenticated `gh` and, for cosign-signed images, `cosign`
on `PATH`:

```bash
python3 scripts/validate_image_provenance.py --consumer-root ../vllm-service
```

**Adding a signer.** When an upstream starts signing, first confirm the
verify command by hand against the digest the federation pins. Then add the
entry, plus that real digest to `tests/fixtures/prov-live/`, so self-ci keeps
proving the entry against the live registry. For a key-based signer, commit
the public key and record its fingerprint
(`openssl pkey -pubin -outform DER | openssl dgst -sha256`) from two
independent sources.

What it does not do:

- **A valid signature is not a safety verdict.** It proves the publisher's own
  pipeline built the image, not that the pipeline was uncompromised. The
  Dependabot cooldown covers part of that gap by letting a bad release be
  caught upstream before it is proposed. That cooldown only ages same-tag
  digest refreshes for Docker Hub images, though; for ghcr.io and other
  registries Dependabot proposes a new digest as soon as it sees it, which
  makes the signature the main check there.
- **Unsigned images cannot be verified at all.** Their pins remain a record of
  what was reviewed, not proof of origin.
- It checks the committed refs. A runtime `.env` that overrides an image
  variable is outside its view.
