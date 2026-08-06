---
mode: agent
description: Regenerate the re-sync design and engine from the schema + locked decisions.
---

You are building a generic production→lower-environment Oracle database re-sync, following
`.github/copilot-instructions.md`, `DESIGN.md` and `CONTEXT.md` in this repo. Work in small steps
and stop for my confirmation at each decision point.

Inputs already in the repo (deterministic, do not regenerate with the model):
- `catalog.json` — SchemaCrawler serialize output for the schema.
- `extract.jq`, `build-config.sh`, `extract-schema.sh` — schema-extraction pipeline.
- `config.skeleton.json` — flattened schema (tables, columns, PK/unique, FK edges).

Tasks:

1. Run `./build-config.sh` and confirm the graph is acyclic; report the parents-first load order
   and any tables lacking an enforced natural key. Do not parse `catalog.json` yourself — rely on
   the jq/tsort output.

2. Draft `resync.yaml`: one entry per in-scope table with `mode` and `identity`, using the
   conventions in the instructions (`*_CD` natural keys; `VERSION_ID`/audit columns excluded; value
   objects keyed on parent FK + discriminator taken from the composite PK). Flag owned children
   (`*_DTL`, `*_LABEL`, key/value detail) with `delete_orphans: true`. Mark any surrogate FK to an
   out-of-scope table as a BLOCKER in a comment.

3. Build the Python engine under `resync_engine/` (`model`, `graph`, `plan`, `sqlgen`, `runner`,
   `cli`) that: loads catalog+config, topologically sorts, detects cycles, propagates surrogate
   lineage for FK remapping, and generates set-based SQL — single `MERGE` for natural/value tables;
   create-idmap/match/allocate/insert/update for surrogate tables; a scoped delete-orphan `DELETE`
   for owned children. Add offline tests in `tests/` that run without a database.

4. Keep `DESIGN.md`, `CONTEXT.md`, and `docs/adr/` in sync as decisions are made.

Verify: `python tests/test_engine.py` passes and
`python -m resync_engine.cli print-sql` renders SQL for every in-scope table offline.
