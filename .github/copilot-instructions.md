# Copilot instructions — database re-sync project

These instructions are auto-loaded into every Copilot Chat request in this repo. They pin the
design so Copilot converges on the same artifacts a prior agent session produced. Read `DESIGN.md`
and `CONTEXT.md` before answering; they are the source of truth.

## What this project is

A generic procedure to re-sync a production Oracle database into lower environments (DEV, TEST,
PERF), copying everything from the source while preserving data created on the target since the
last re-sync. It is **not a copy** — surrogate keys differ per environment, so it is a merge keyed
on **natural identity**, with foreign-key remapping through per-entity id-maps, processed
parent-first in topological order.

## Invariants (never violate)

1. Every in-scope row has a stable, environment-portable natural identity — never the surrogate key.
2. A surrogate value crosses environments only through a `source → target` id-map built by matching
   on natural identity; every FK is translated through it.
3. Identity resolves bottom-up: children after parents. The entity graph must be acyclic.

## Locked decisions (do not re-litigate; see DESIGN.md decisions table)

- Preserve target **inserts** only, at the **aggregate-root grain**. Owned children
  (`delete_orphans: true`) follow their root: mirror within a source-matched parent (delete
  orphans), preserve the whole subtree under a target-only parent.
- **Stateless** — no baseline snapshot; source deletes are not propagated.
- Identity comes from a **reviewed YAML config** (`resync.yaml`), seeded from constraints, finished
  by hand. `*_CD` columns are natural keys by convention. `VERSION_ID` and `UPDATE_*`/`INSERT_*`/
  `*_IND` are audit columns — never identity.
- **No PII masking** (lower envs cleared for verbatim prod data).
- Topology B: staging schema on target, Data Pump file handoff, set-based `MERGE` in-database.
- Engine is Python generating in-DB SQL. Snapshot via Flashback SCN. Dry-run then atomic apply.
- Cycles: detect always, fail loud; null-then-update only for nullable back-edges.

## Conventions

- Scope is the in-scope base tables named in the private `INSTANCE.md`. Isolated tables and
  `*_V` views are out of scope. (The public sample uses the Order/OrderLine schema in `examples/`.)
- Matching modes per table: `natural | value | hash | out_of_scope`.
- Strip `SYS_NC%$` hidden columns (function-based-index virtual columns).
- Do **not** ask the model to parse `catalog.json` (600 KB) — use the deterministic `jq`/`tsort`
  pipeline for all schema facts. The model only authors judgment (identity choices, docs, code).
