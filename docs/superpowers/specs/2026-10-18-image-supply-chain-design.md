# Image supply-chain checks

**Date:** 2026-10-18
**Status:** implemented
**Sensitivity:** This document is in a public repo. It names public upstream
images, public member repos and file paths only. Keep host topology, airgap
mechanics and deployment specifics out of it, as with all prose here.

## Problem

Every federation repo pins its container images by digest
(`image:tag@sha256:…`), and Dependabot proposes digest refreshes as PRs. The pin
makes each change explicit and reviewable, but nothing helped judge a change.
A digest bump says "the tag now points here" and nothing else. Three
questions were left to whoever reviewed the PR:

1. Did the publisher build this digest, or did someone else push it under the
   tag?
2. Has the release been out long enough for a compromise to be noticed
   upstream?
3. Does the new image fix or introduce known vulnerabilities?

CI answered none of them. The apps' docker job proved that the images build,
but ran the tests outside them; no job scanned an image; no job checked a
signature, although several upstreams sign.

## The findings that shaped the design

- **Dependabot vouches for nothing.** It reads the registry and proposes the
  current digest. If a tag were hijacked, the PR would look exactly like a
  routine refresh.
- **Cooldown only reaches Docker Hub.** Dependabot (dependabot-core,
  `docker/lib/dependabot/docker/update_checker.rb`) does apply its cooldown to
  same-tag digest refreshes, but takes the publication date only from Docker
  Hub's `tag_last_pushed`. For ghcr.io, nvcr.io, lscr.io and other registries
  it logs that no date source exists and skips the cooldown. The default
  3-day cooldown therefore protects the Docker Official Images and nothing
  hosted elsewhere.
- **Few upstreams sign.** Of the eighteen non-official upstream image repos the
  federation pins, four had verifiable signatures at their pinned digests:
  - uv: GitHub artifact attestations;
  - LiteLLM: cosign with a published key, logged in Rekor;
  - Qdrant: keyless cosign, from a tag-triggered workflow on self-hosted
    runners;
  - the NVIDIA DCGM exporter: cosign with NGC's key, not logged in any
    transparency log.

  Docker Official Images carry no verifiable signature since Content Trust
  was retired. They do carry unsigned BuildKit attestations, which prove
  nothing about origin.
- **Signatures sit on the index digest.** For all four, the attestation or
  signature covers the multi-arch index, which is what the federation pins.
  Verifying a per-platform child digest fails.
- **The uv signer moved.** uv's older images were signed by `build-docker.yml`,
  its current ones by `publish-docker-image.yml`. The policy therefore pins the
  signer repo rather than the workflow file.

## Design

### Provenance check

`scripts/validate_image_provenance.py` is a policy-flavor validator. Like the
pin checks it has no canonical file to mirror; it checks the consumer's own
Dockerfiles and compose files. It runs in `python-app-ci`'s lint job and
`infra-validation`'s make-common job, ref-locked like the other validators.

The policy, `configs/image-provenance/signers.toml`, lists the signed images
and how to verify each one: `github-attestation`, `cosign-key` (with the
public key committed under `configs/image-provenance/keys/`) or
`cosign-keyless`. It is read in place from the pinned hub revision, never
vendored, so a consumer is always checked against the policy that shipped with
the revision it pinned.

The rules:

- Every digest-pinned ref to a listed image must verify.
- A listed image pinned without a digest fails.
- Unlisted images are reported as unsigned in the job summary.
- Each distinct `name@digest` is verified once, however many files pin it.

`gh attestation verify` reads attestations with the job's own read-only
`github.token`, so callers need no new permissions. self-ci proves this with
the same token scope.

### Failing closed around the parser

The script parses by line, like every validator here, so it can miss syntax it
does not model. A background security review of the first version pointed out
three ways a listed image could slip past the parse:

- a variable in the ref;
- a YAML block scalar or flow mapping;
- a Dockerfile `escape` directive.

Rather than trust the parse, the check fails closed:

- A listed image name on any non-comment line that was not read as part of a
  reference fails. Continuation lines and the `ARG` default lines that feed a
  `FROM` count as part of the reference.
- A ref whose unresolved variable could stand for a listed image fails: the
  text before the first `$` is a prefix of a listed name, or names one.
- The `escape` directive is honored.

### Exit codes

0 means everything verified, 1 is a policy failure, 2 means the check could
not run. Verifier output is classified by known policy-failure markers
*first*. An identity-mismatch message echoes the identity it found, and the
image's publisher controls that text; checking network patterns first would
let a subject URI containing "timeout" relabel a mismatch as an outage. In
consumer CI both non-zero codes fail the job, so the label can never turn a
failure into a pass.

### Image scan

`scripts/scan_built_images.py` runs in the docker job after
`docker compose build`. It scans every service compose built, from the local
image store, with Grype, and writes per-image counts and the fixable findings
to the job summary.

The gate fails only on *fixable* findings at or above `docker-scan-fail-on`,
default `critical`. An unfixed vulnerability is reported and never blocks,
because there is nothing to bump to. A consumer's `.grype.yaml` ignore rules
apply, each with a reason, and ignored matches are counted in the summary so
an exception stays visible.

### Cooldown

Each consumer's Dependabot entries get an explicit 7-day cooldown, as do the
hub's own. Security updates are exempt. The `github-actions` entries in hub
consumers exclude `nos-tromo/*`, so a hub release is not held back a week.

## Limits, stated plainly

- **A valid signature proves origin, not intent.** If a publisher's own
  pipeline is compromised, its signatures still verify. The cooldown is the
  only mitigation, and it does not reach non-Docker-Hub registries.
- **Most pinned images are unsigned.** For those, the check can only say so.
- **The scan reflects the vulnerability database on the day it runs.** A red
  docker job on an unchanged branch usually means a new advisory.
- **Committed refs only.** Runtime environment overrides of an image variable
  are not seen.
- **Live dependencies.** The checks depend on GitHub's attestation API, the
  Sigstore infrastructure, the registries, and Grype's database CDN. Outages
  surface as exit 2.

## Rejected alternatives

- **Trivy as the scanner.** Its action tags, setup action and release binaries
  were compromised in March 2026 (CVE-2026-33634). Commit-SHA pins would have
  blocked the tag rewrite but not a malicious binary download. Grype's action
  pins the scanner release per action version and fetches its installer at
  that release tag.
- **Report-only scanning.** A summary nobody is forced to read decays into
  noise. Gating only on fixable criticals keeps the gate rare and actionable.
- **Gating on any severity, or on unfixed findings.** That would keep CI red
  on advisories with nothing to bump to.
- **Verifying uv with cosign.** uv's attestations live in GitHub's attestation
  store, not in the registry, so cosign alone cannot find them.
- **Reading `.env` for compose interpolation.** CI sees committed defaults,
  and a runtime `.env` differs per host by design.
