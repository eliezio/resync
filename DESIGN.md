# Production → Lower-Environment Database Re-Sync — Design

## Context

We periodically refresh lower environments (DEV, TEST, PERF) from production. A plain copy is
wrong for two reasons:

1. **Surrogate keys differ per environment.** Primary keys are Oracle sequence values, so the
   same logical row has a different key in production than in DEV. Rows cannot be matched
   across environments by primary key, and foreign-key columns copied from production point at
   key values that mean nothing in the target.
2. **We must preserve data created directly on the target** since the last re-sync (e.g. test
   fixtures) — a straight import would destroy it.

So the operation is a **three-way-flavoured merge keyed by natural identity**, not a copy.
This document records the strategy, the decisions behind it, and the target architecture.

Target platform: **Oracle 19**. Every mechanism below is native to it.

## General strategy

The reusable core, independent of any particular schema.

**Problem shape.** Copying a database whose identity is a per-environment surrogate key is not a
copy — it is a **merge keyed on natural identity**. Surrogate keys carry no meaning across
environments, and foreign keys built on them are noise until translated. Preserving
target-created data makes it a merge rather than a load.

**Three invariants** everything rests on:

1. **Every row has a stable, environment-portable identity** — a natural key, not the surrogate.
   A table that cannot state one cannot be merged; it can only be reloaded or skipped.
2. **A surrogate value identifies the same row across environments only through a mapping.** Build
   a `source → target` id-map per entity by matching on natural identity, and translate every
   foreign key through it.
3. **Identity resolves bottom-up.** A child's identity may depend on its parent's, so identities —
   and therefore the whole merge — are computed **parent-first**. That requires an acyclic entity
   graph.

**Generic algorithm** (any schema satisfying the invariants):

```
extract a consistent source snapshot into staging (source keys intact)
topologically order the entities (fail on cycle)
for each entity, parents-first:
    match staging rows to target rows on natural identity  -> id-map
    remap FK columns through parents' id-maps
    MERGE: update matched · insert unmatched (new target keys) · leave target-only
verify referential integrity · preserve target-only rows · confirm idempotent
```

**Deliberate scoping choices** — each trades completeness for simplicity and is tunable per
project (the specifics chosen here are in the decisions table below):

- Preserve target **inserts** only, not edits or deletes — avoids a conflict policy. Preservation
  is at the **aggregate-root** grain: owned children follow their root (Source-matched root →
  children mirrored, orphans deleted; target-only root → whole subtree preserved).
- **Stateless** (no baseline) — no source-delete propagation; stale rows tolerated.
- A **reviewed identity config** is the source of truth — automation seeds it, humans finish it.
- Per-table **escape hatches**: reload (source-owned, exact mirror), hash (wide immutable value
  rows), out-of-scope.

**Why bespoke.** No off-the-shelf tool does natural-key matching, FK remapping, and target-row
preservation together; commodity tools cover only the edges (schema extraction, data transport).

## Strategy in one paragraph

Extract a transaction-consistent snapshot of the source into a throwaway **staging schema on
the target database**. Process tables **parent-first** in topological order. For each table,
match staging rows to live target rows by **natural identity**, building an **id-map**
(`source surrogate → target surrogate`). Rewrite each child's foreign-key columns through its
parents' id-maps, then `MERGE`: update matched rows from source, insert unmatched source rows
(assigning fresh target-side surrogate keys), and **leave target rows that have no source
match** — those are the preserved target-only rows.

## Decisions

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | What target changes to preserve | **New rows only** | Preserving edits/deletes of source-owned rows creates true merge conflicts needing a policy; inserts are safe and cheap. |
| 2 | Handle rows deleted on source | **Stateless, no baseline** | No cross-run state. Source-deleted rows are indistinguishable from new target rows and are kept (stale accumulation accepted). See ADR-0001. |
| 3 | Source of natural identity | **Reviewed config, seeded from constraints** | Natural keys are frequently not constraint-enforced in this schema; a reviewed file is the only trustworthy source. `*_CD` columns default to identity by convention. |
| 4 | PII masking | **None** | Lower environments cleared to hold verbatim production data. (Compliance risk flagged and accepted.) |
| 5 | Cycles | **Detect always; null-then-update for nullable back-edges; fail loud otherwise** | Implemented: `graph.load_order` breaks a nullable back-edge (or nullable self-reference), records it as *deferred*; the deferred FK column is inserted `NULL` and set in a second-pass `MERGE` (`sqlgen.deferred_update`) after all rows exist. Non-nullable cycles raise `CycleError`. The current schema is acyclic, but this guards against drift. |
| 6 | Execution topology | **Staging schema on target; Data Pump file handoff; local set-based MERGE** | Merge logic runs in-DB (fast, transactional); production is read-only; a dump-file handoff needs no live network path from a lower environment into production. |
| 7 | Identity-less tables | **Per-table mode: natural / value / hash / reload / out_of_scope** | Forces every table to a conscious classification; no silent behaviour. |
| 8 | Engine runtime | **Python orchestrator generating in-DB SQL** | Config parsing, topological sort, cycle detection and validation in Python; heavy data work as set-based `MERGE` executed via `python-oracledb`. |
| 9 | Snapshot consistency | **Flashback SCN** (`expdp FLASHBACK_SCN`) | One consistent SCN across all tables; no production downtime. Requires adequate UNDO retention. |
| 10 | Safety | **Dry-run then apply, single transaction** | Per-table insert/update/preserved counts logged before the MERGE; the whole run commits or rolls back atomically. |
| 11 | Config format | **YAML** | Human-reviewed, comments document why each identity was chosen. |
| 12 | Owned children | **Scoped delete-orphan** | An owned Value Object (e.g. `*_DTL`, `*_LABEL`) follows its aggregate root: within a Source-matched parent the Source child set is authoritative, so Target children absent from Source are deleted. Children under target-only parents are preserved. Refines decision 1: "preserve target-only" applies at the aggregate-root grain. |

## Open-source tooling

No off-the-shelf tool does natural-key merge with foreign-key remapping *and* target-row
preservation. Data Pump, GoldenGate and comparison tools all assume matching keys or do full
replication. Therefore the merge engine is **bespoke**. Open source is used only for the parts
that are commodity:

- **Schema extraction** — SchemaCrawler (`--command=serialize --output-format=json
  --info-level=standard`) or the native data-dictionary queries (`ALL_CONSTRAINTS`,
  `ALL_CONS_COLUMNS`). SchemaCrawler also renders the ER diagram and runs a cycle linter
  (`LinterTableCycles`).
- **Cycle detection / topological sort** — Unix `tsort` on the FK edge list (errors on a
  cycle), as a zero-dependency cross-check of the Python implementation.
- **Data movement** — Oracle Data Pump (`expdp`/`impdp`).

The FK graph and identity seed are produced by [`extract.jq`](./extract.jq) against the
SchemaCrawler catalog, yielding [`config.skeleton.json`](./config.skeleton.json).

## Architecture

```
 production (read-only)                target database (DEV/TEST/PERF)
 ┌───────────────────┐   dump file    ┌───────────────────────────────┐
 │ expdp             │  ──────────▶   │ impdp → STAGING schema         │
 │ FLASHBACK_SCN=…   │                │   (source rows, source keys)   │
 └───────────────────┘                │                                │
                                      │ Python orchestrator            │
                                      │   1. load identity config      │
                                      │   2. topo sort + cycle check   │
                                      │   3. per table, parent-first:  │
                                      │        build id-map (match on  │
                                      │        natural identity)       │
                                      │        remap FK columns        │
                                      │        MERGE into live table   │
                                      │   4. dry-run report, then apply│
                                      │ (single transaction)           │
                                      │ drop STAGING                    │
                                      └───────────────────────────────┘
```

### Identity config (YAML)

```yaml
# seeded from config.skeleton.json, completed by hand (Order/OrderLine sample)
tables:
  CURRENCY:
    mode: natural
    identity: [CURRENCY_CD]           # *_CD convention
  SALES_ORDER:
    mode: natural
    identity: [ORDER_NO]              # VERSION_ID excluded (bumped in place)
    sequence: ORDER_SEQ
  ORDER_LINE:
    mode: value                       # identity references the parent order
    identity: [ORDER_ID, LINE_NO]
    delete_orphans: true              # owned child of SALES_ORDER
```

See `examples/sample_resync.yaml` for the full worked example.

Fields per table: `mode` (natural | value | hash | reload | out_of_scope), `identity`
(column list), `hash_exclude`, and any manually declared foreign-key edges not enforced by a
constraint.

### Algorithm per table (parent-first)

1. **Resolve foreign keys.** For each FK column, look up the parent's id-map to translate the
   staged source key into the target key. Value-Object and hash identities that reference a
   parent use the parent's *natural* key.
2. **Match.** Join staging to the live target table on natural identity. Matched pairs seed the
   id-map (`source key → existing target key`).
3. **Merge.**
   - *matched* → `UPDATE` all non-identity columns from staging.
   - *unmatched source* → `INSERT`, taking a fresh key from the target's own sequence; record
     `source key → new target key` in the id-map.
   - *unmatched target* → leave untouched (preserved target-only row).
4. **Owned children** (`delete_orphans: true`): after the merge, a scoped `DELETE` removes Target
   children whose owning parent is Source-matched (its surrogate is in the id-map) but whose
   identity is absent from Source. Children under target-only parents are never in the id-map, so
   their subtree is preserved. Oracle `MERGE` cannot delete not-matched-by-source rows, so this is
   a separate statement.
5. `reload` tables skip matching: `TRUNCATE` then insert all staged rows with remapped FKs.
6. **Deferred FK second pass** (nullable cycles / self-references): FK columns dropped to break
   a cycle are inserted `NULL`, then set by a `MERGE` after every table is loaded, so both
   endpoints exist.

**Sequences.** Each surrogate table declares its target `sequence:`. New rows allocate keys via
`sequence.NEXTVAL`, which advances the sequence past every id the engine assigns — so keys stay
monotonic, never collide with preserved target rows, and the application's next insert cannot
collide either. No separate "advance" step is needed. Applying is **refused** if any surrogate
table lacks a sequence (`unsequenced_surrogates`): the fallback (`MAX(id) + ROWNUM`) does not
advance the real sequence and is dev-only, reachable via `--allow-unsequenced`.

### Referential integrity during load

Target FK constraints are loosened for the merge via `constraint_handling` (global config):

- **`disable`** (default) — `ALTER TABLE … DISABLE CONSTRAINT` before the merge, re-enabled
  `ENABLE VALIDATE` after, so a residual violation aborts. Note these are **DDL**, so each issues
  an implicit `COMMIT`; the merge itself is one transaction between the DDL bookends, and full-run
  atomicity relies on the pre-run target backup (see the runbook). On failure the engine re-enables
  `NOVALIDATE` so constraints aren't left disabled.
- **`defer`** — `SET CONSTRAINTS ALL DEFERRED`; validated at `COMMIT`, so the entire run is one
  atomic transaction. Requires the FK constraints to be **DEFERRABLE**.
- **`none`** — leave constraints as-is; for an acyclic graph the topological order already inserts
  parents before children.

Only *enforced* FK constraints are touched (`resync_engine/plan.py:fk_constraints`); manually
declared `manual_fks` have no database constraint. Topological order still governs id-map
availability regardless (disabling constraints does not remove the parent-before-child requirement).

## Applying to a schema

The public repo carries a synthetic worked example (Order/OrderLine) in `examples/`:
`sample_catalog.json` (schema in `config.skeleton.json` format) and `sample_resync.yaml`
(the identity config). It exercises every feature — reference tables, a surrogate root with an
in-place `VERSION_ID`, owned children with delete-orphan, surrogate-lineage remap, and
FK-to-code. Run `python -m resync_engine.cli print-sql` to see the generated SQL.

To apply to a real schema:

1. Extract with `extract-schema.sh` + `build-config.sh` → `config.skeleton.json` (gitignored).
2. Author `resync.yaml` (gitignored) from the skeleton: `*_CD` columns are natural keys by
   convention; exclude `VERSION_ID`/`UPDATE_*`/`INSERT_*`/`*_IND`; ignore `SYS_NC%$` hidden
   columns; value objects key on parent FK + discriminator (usually the composite PK); flag
   owned children `delete_orphans: true`; declare a `sequence:` per surrogate table.
3. Environment-specific notes (scope, real roots, load order, blockers) live in a private,
   gitignored `INSTANCE.md` — never in the public repo.

**Audit-exclude set** (never in any identity, excluded from every hash): `UPDATE_ID`,
`UPDATE_TMSTMP`, `INSERT_ID`, `INSERT_TMSTMP`, `VERSION_ID`, `*_IND` soft-delete flags.

## Known limitations

- **Source deletes never propagate** (stateless model) — stale rows accumulate on the target.
  See ADR-0001.
- **Hard cell:** a table that both takes target-only rows *and* mutates *and* has no natural
  key is unsolvable under the stateless model — **verify** such a table is absent before build.
- **Logical-only relationships** (code columns not enforced by an FK) are invisible to the
  extraction; declare them by hand in the config (`manual_fks`) or the table stays BLOCKED.

## Engine

Implemented in [`resync_engine/`](./resync_engine):

- `model.py` — load catalog (`config.skeleton.json`) and config (`resync.yaml`).
- `graph.py` — Kahn topological sort, `CycleError` on any cycle.
- `plan.py` — classify surrogate vs natural identity; propagate **surrogate lineage** so a
  surrogate value is remapped through its *origin* table's id-map wherever it reappears.
- `sqlgen.py` — generate set-based SQL: single `MERGE` for natural/value tables; a 5-step
  id-map sequence (create / match / allocate / insert / update) for surrogate tables.
- `runner.py` — dry-run counts, then apply all DML in one transaction; drop id-maps after.
- `cli.py` — `print-sql` (offline preview) and `run [--apply]`.

```
python -m resync_engine.cli print-sql            # runs the public sample by default
python -m resync_engine.cli run --catalog config.skeleton.json --config resync.yaml --dsn … --user … --password …
python -m resync_engine.cli run --catalog … --config … --dsn host:1521/svc --user MAS --password *** [--apply]
```

The engine refuses to run while any table is BLOCKED (an identity column that is a surrogate FK
to an out-of-scope table, with no resolvable id-map). Offline tests: `tests/test_engine.py`.

## Runbook

One re-sync run, end to end. Steps 1–5 are side-effect-free; step 6 is atomic.

**0. Preconditions**
- `resync.yaml` reviewed; no BLOCKED tables; `sequence:` declared per surrogate table.
- Target environment quiesced (application stopped or read-only) — the merge mutates live tables.
- Production UNDO retention covers the extract duration (for the Flashback snapshot).

**1. Pin snapshot (production, read-only)**
- Capture `scn := CURRENT_SCN`.
- `expdp` the in-scope tables with `FLASHBACK_SCN=<scn>` to a dump file — one consistent point.

**2. Transfer**
- Move the dump file to the target host (file handoff; no live production↔lower link).

**3. Stage (target database)**
- Drop and recreate the `RESYNC_STG` schema.
- `impdp REMAP_SCHEMA=<prod>:RESYNC_STG` — raw source rows carrying source surrogate keys.

**4. Guard**
- Snapshot the target: per-table row counts and the count of presumed target-only rows, for the
  post-run preservation check.
- Disable or defer target foreign-key constraints.

**5. Dry-run**
- `python -m resync_engine.cli run … ` (without `--apply`): per-table matched / insert /
  target-only counts.
- Human gate — sanity-check the numbers. A table showing all-insert signals a misconfigured
  identity; stop and fix `resync.yaml`.

**6. Apply (single transaction)**
- Parents-first per the load order. Each table: build its id-map, remap FK columns, `MERGE`
  (update matched, insert new with fresh keys, leave target-only rows untouched).
- Advance each surrogate sequence past `MAX(id)`.
- Re-enable FK constraints **WITH VALIDATE** — any breakage aborts the transaction.
- Commit, or roll the whole run back on any error.

**7. Verify** (see the verification section below).

**8. Cleanup**
- Drop the id-map tables and the `RESYNC_STG` schema.
- Re-open the target to the application.

**Rollback points:** steps 1–5 leave no trace (drop staging and stop). Step 6 is atomic —
commit or full rollback. After a committed run, recovery is either a re-run (idempotent) or
restoring the target from its pre-run backup.

## Verification

1. **Extraction:** `jq -f extract.jq catalog.json` yields 0 null parent/child; `tsort` reports
   acyclic.
2. **Dry-run:** engine prints per-table insert/update/preserved counts; sanity-check totals.
3. **Round-trip test on a scratch schema:**
   - Seed a target from a source snapshot.
   - Insert known target-only rows; edit and delete some source rows.
   - Re-run; assert: target-only rows survive; source edits applied; every FK resolves
     (`re-enable constraint … validate` succeeds); no orphaned surrogate keys.
4. **Idempotency:** run twice back-to-back; the second run reports 0 inserts / 0 updates.
