# Versioning

Workflows are released as immutable minor tags (`v3.1`, `v3.2`, …) with a
moving major alias (`v3`) that always points at the latest `v3.x`. Consumers
pin the **commit SHA** the tag points at, not the tag itself (see
[pinning.md](pinning.md#action-refs)) — as the README caller snippets do —
and keep the minor version in the trailing comment so it stays readable.
Dependabot's `github-actions` ecosystem opens a bump PR when a newer tag ships,
rewriting SHA and comment together. The tags remain the release mechanism and
the unit a bump PR is named after; the major alias is what anything still
referencing `@v3` resolves through.

Cutting a tag has **two** steps — the second is easy to forget and silently
strands anything on `@v3` at the old commit:

1. Tag the merge commit with the next minor —
   `git tag -a v3.12 -m "v3.12: …" && git push origin v3.12`
2. Move the major alias to the same commit —
   `git tag -f -a v3 -m "v3: …" && git push origin v3 --force`

Because the `python-app-ci` lint job validates each consumer against the
strict config that shipped with the tag it runs, a canonical-config change
and the consumers' mirrored-config updates must land together (see
[strict-python.md](strict-python.md)) or the consumers'
lint jobs fail. The full tag list is on the
[tags page](https://github.com/nos-tromo/.github/tags).
