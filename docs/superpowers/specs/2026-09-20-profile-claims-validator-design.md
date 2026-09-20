# Profile claims validator

**Date:** 2026-09-20
**Status:** implemented
**Sensitivity:** This document is in a public repo. It names member file paths
and regexes only; every repository it references is public. Keep host topology,
airgap mechanics and deployment specifics out of it, as with all prose here.

## Problem

`profile/README.md` is the account's public landing page. It describes twelve
sibling repositories, which makes it the only file in this repo whose accuracy
depends on code none of our CI can see. Nothing checked it.

Measured from git history on 2026-09-20:

| Drift class | Instances | How long wrong |
|---|---|---|
| Membership (a new repo not yet listed) | deploy, obs-plane, infra-ui, edge-plane | 0, 0, 11, 10 days |
| Claims (a stated fact is false) | TOTP, TranslateGemma | **49 and 55 days** |

Membership drift is frequent but self-correcting, because adding a repo is a
visible act. Claim drift is the expensive kind, and both instances survived the
dedicated accuracy audit of 2026-08-23 (#54).

## The finding that shaped the design

**An anchor into a member's prose false-passes.**

The page claimed Authelia enforced TOTP. The string "TOTP" appears in two
edge-plane documents, as a future option. It appears zero times in
`authelia/configuration.yml`, where every access rule is `one_factor`. A
validator that greps the member's docs therefore goes green on the false claim,
which is worse than no validator: it manufactures confidence.

Anchoring to config works, and works with day resolution. translator's
`docker/compose.yaml` named a `translate-gemma` default on 2026-07-26 and did
not on 2026-07-27. Reconstructing the historical claim and running it either
side of that commit gives exit 0 then exit 1. The 55-day window would have been
one cron interval.

There is a second, quieter benefit. An author who must anchor a claim to a line
of config has to find that line. For TOTP there was none. The claim would have
failed at authoring time.

## Design

`scripts/validate_profile_claims.py` plus `profile/claims.toml`. Stdlib-only,
Python 3.11+, no network: callers supply member checkouts on disk, exactly as
`validate_strict_config.py --consumer-root ../chorus` already works.

### Three-valued exit

`0` holds, `1` drift, `2` could-not-run. This is the only script here with more
than two outcomes, and it exists so the scheduled job never opens a drift issue
because a clone failed or the interpreter raised. `main()` is wrapped so an
unexpected exception exits 2, and every smoke step asserts the exact code
rather than "non-zero", which would let a traceback pass as a detected drift.

### Claims carry a quote

Coverage counted per repository is not enough: one trivial anchor per row
satisfies it while the sentence beside it rots. Each claim therefore names a
verbatim `quote` from its table cell, and the check fails when the quote leaves
the row. Rewriting the prose reddens the pull request instead of silently
orphaning the anchor.

### `repo` and `in` are different things

`repo` is the row the sentence lives in. `in` lists the checkouts holding the
evidence. They diverge for cross-repo claims: "the one member that publishes
ports at all" is edge-plane's sentence, but its evidence is the *absence* of a
`ports:` block in eight other repos.

### Five assertion kinds

`matches`, `absent`, `json_len`, `glob_count` and `exists`. Enough for the
twenty-eight anchors the page needs; deliberately not a query language.

### Guards against anchors that pass for the wrong reason

- **Prose is refused.** A `.md` target is rejected unless the claim sets
  `allow_prose`, which makes any exception visible in review.
- **Comment lines are skipped** by default, as `validate_action_pins.py`
  already does. A comment inside config is prose, and the same false-pass
  applies: edge-plane's compose names `inference-net` in a `###` banner.
- **A hit ceiling** (default 2) rejects anchors so generic they prove nothing.
  This fired during implementation: `request_header -X-Auth-` matched four
  lines, which turned out to be the four contract headers rather than a sloppy
  regex, so the claim was pinned at exactly four and now asserts something
  stronger.
- **A non-vacuity floor** on globs: a pattern matching no files fails rather
  than counting zero, so a renamed directory cannot satisfy `eq = 0` forever.
- **Exact path components**, checked against `os.listdir`. `Path.exists` is
  case-insensitive on macOS, so `nextext` resolves locally and fails on Linux;
  without this a claim would pass for the author and open a false issue at 06:17
  on Monday.

Vacuity is ultimately semantic and no mechanical guard closes it. Reviewing
anchors stays a human job.

### Page parsing

Table rows are matched line-anchored; an unanchored link scan picks up `babel`
and `txt2pdf` from the "Other public projects" bullets. Diagram node ids are
arbitrary handles (`webui`, `vllm`), so the repository name is read from the
label before `<br/>`, and a node counts as a repository only if it has that
separator, which excludes the `Users` actor without an allowlist. Both
directions are checked, so a misspelled label fails as an unknown member as
well as a missing node.

Repositories deliberately absent from the diagram sit under a
`<!-- profile:no-diagram -->` marker in their section, rather than in an
exemption list in the validator. A future glue repo is then exempt
automatically. This follows the hub's existing include-driven, self-maintaining
pattern.

## Why the cross-repo half is scheduled, not a gate

The page goes stale when a *member* changes, which has nothing to do with
whichever pull request is open here. Gating on it would turn an unrelated
dependency bump red because someone renamed a file elsewhere — the "every
consumer went red at once" failure this repo already learned once.

So the work is split:

- `self-ci.yml`'s `profile-smoke` gates every PR on what is knowable here: the
  fixtures, the page's structure, quote coverage, and the claims about this
  repository. A PR that adds a workflow or a validator fails immediately rather
  than a week later.
- `profile-audit.yml` clones the members weekly and runs the full audit,
  reporting to one rolling issue. Pull requests touching the page or the
  validator run it too, with issue reporting off.

This is the hub's first `schedule:` trigger. It is free because the repo is
public. GitHub disables scheduled workflows after 60 days of repository
inactivity; that only bites during a long absence and is re-enabled by hand, so
do not add keepalive commits.

## Limits, stated plainly

Green means "every anchored fact still holds", not "the page is accurate". The
validator cannot judge prose, and a claim nobody anchored is a claim nobody
checks. Of the five errors corrected in #66 it would have caught four; the miss
is semantic, where the page said docint does "graph retrieval" and it does
graph-assisted query expansion. Both satisfy any anchor on `GRAPHRAG_ENABLED`.

## Rejected alternatives

- **Grep the member's docs for the claimed term.** False-passes the exact
  errors that prompted the work. Rejected on measurement, not taste.
- **Generate the page from each member's own one-liner.** Architecturally
  tidier, but it relocates staleness into twelve fields that are *less*
  maintained than the page: the GitHub descriptions read "Proof of concept RAG
  application" for a v2.5.2 stack, and are empty for two members.
- **Pin each row to a member docs commit and flag churn.** READMEs change
  constantly, so every row would be permanently "stale". A triage aid, not a
  check.
