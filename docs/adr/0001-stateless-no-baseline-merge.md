# 1. Stateless natural-key merge (no baseline snapshot)

Date: 2026-08-05

## Status

Accepted

## Context

The re-sync must preserve rows created directly on a target environment since the last run,
while refreshing everything that came from production. Surrogate keys differ per environment,
so rows are matched across environments by natural identity.

A row that came from production at the last re-sync and was then **deleted in production** is,
on the target, indistinguishable from a genuinely new target-only row: in both cases its
natural key is absent from the current source. Telling them apart requires remembering what was
delivered last time — a **baseline snapshot** of natural keys per table, giving a true
three-way merge.

We considered:

- **Baseline (three-way).** Store a manifest of delivered natural keys per environment. Correctly
  propagates source deletes (keys in the baseline but not in the current source are deleted from
  the target). Cost: per-environment versioned state that must be stored, kept in step with each
  run, and recovered if a run fails midway.
- **Stateless.** Keep no cross-run state. Rule: natural key in source → refresh; not in source →
  keep. Simple and recoverable, but source-deleted rows masquerade as target-only rows and are
  never removed.

## Decision

Use the **stateless** model. No baseline is stored. A target row whose natural identity is not
present in the current source is always preserved.

## Consequences

- No cross-run state to store, version, or repair; a run depends only on the current source
  snapshot and the current target.
- **Source deletes do not propagate.** Rows deleted in production remain on lower environments
  and accumulate as stale data over successive re-syncs.
- The definition of "target-only row" is therefore *heuristic*, not exact — it includes both
  genuinely new target rows and orphaned formerly-production rows.
- Tables that are Source-owned and must mirror production exactly (including deletes) are not
  covered: the merge never deletes Source-absent target rows. A dedicated `TRUNCATE`+reload path
  was considered and dropped as unused; reintroduce it if such a table appears.
- If stale accumulation later proves unacceptable, revisit by introducing a baseline manifest;
  this is a reversible-at-cost decision, hence recorded here.
