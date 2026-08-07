"""Execute a re-sync against Oracle, or preview the SQL offline.

Flow (see DESIGN.md): the staging schema is assumed already loaded (Data Pump). This engine
does the merge: dry-run counts, then — if applying — all DML in a single transaction that
commits or rolls back atomically. Id-map tables are created in the staging schema and dropped
at the end.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import sqlgen
from .model import Config, Schema
from .plan import TablePlan, build_plans, fk_constraints, unsequenced_surrogates


@dataclass
class Counts:
    matched: int
    to_insert: int
    target_only: int


def plan_all(catalog_path: str, config_path: str
              ) -> tuple[Schema, list[str], dict[str, TablePlan], Config]:
    schema = Schema.from_catalog(catalog_path)
    config = Config.from_yaml(config_path)
    order, plans = build_plans(schema, config)
    return schema, order, plans, config


def print_sql(catalog_path: str, config_path: str) -> str:
    """Render all generated SQL without a database connection (offline verification)."""
    _schema, order, plans, cfg = plan_all(catalog_path, config_path)
    out: list[str] = []
    for name in order:
        p = plans[name]
        out.append(f"-- ===== {name}  (mode={p.mode}, surrogate={p.surrogate}) =====")
        if p.unresolved:
            out.append(f"-- BLOCKED: unresolved surrogate columns {p.unresolved} — see resync.yaml")
        for stmt in sqlgen.statements(p, cfg.staging_schema, cfg.target_schema):
            out.append(stmt + ";\n")
    deferred = [n for n in order if plans[n].deferred_cols]
    if deferred:
        out.append("-- ===== deferred FK second pass (broken nullable cycles / self-refs) =====")
        for name in deferred:
            du = sqlgen.deferred_update(plans[name], cfg.staging_schema, cfg.target_schema)
            if du:
                out.append(du + ";\n")
    return "\n".join(out)


def _scalar(cur, sql: str) -> int:
    cur.execute(sql)
    return int(cur.fetchone()[0])


def _counts(cur, p: TablePlan, stg: str, tgt: str) -> Counts:
    """Counts assume any id-maps are already built (runner phase A).
    Surrogate tables read their id-map directly; others fall back to the dry-run count query."""
    if p.needs_idmap:
        im = sqlgen.idmap_name(stg, p.name)
        matched = _scalar(cur, f"SELECT COUNT(*) FROM {im} WHERE IS_NEW = 0")
        to_ins = _scalar(cur, f"SELECT COUNT(*) FROM {im} WHERE IS_NEW = 1")
        total = _scalar(cur, f"SELECT COUNT(*) FROM {tgt}.{p.name}")
        return Counts(matched, to_ins, total - matched)
    if p.mode == "reload":
        return Counts(0, _scalar(cur, f"SELECT COUNT(*) FROM {stg}.{p.name}"), 0)
    cur.execute(sqlgen.dryrun_counts(p, stg, tgt))
    m, i, t = cur.fetchone()
    return Counts(int(m), int(i), int(t))


def run(catalog_path: str, config_path: str, dsn: str, user: str, password: str,
        apply: bool = False, allow_unsequenced: bool = False) -> None:
    import oracledb  # lazy: not needed for print_sql

    schema, order, plans, cfg = plan_all(catalog_path, config_path)

    blocked = [n for n in order if plans[n].unresolved]
    if blocked:
        raise SystemExit(f"refusing to run: blocked tables {blocked} (see resync.yaml)")

    conn = oracledb.connect(user=user, password=password, dsn=dsn)
    conn.autocommit = False
    cur = conn.cursor()
    # Deterministic rendering so hash-mode canonicalization (TO_CHAR) is stable across sessions.
    for _stmt in ("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'",
                  "ALTER SESSION SET NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF6'",
                  "ALTER SESSION SET NLS_NUMERIC_CHARACTERS = '.,'"):
        cur.execute(_stmt)
    stg, tgt = cfg.staging_schema, cfg.target_schema
    try:
        if apply:
            unseq = unsequenced_surrogates(order, plans)
            if unseq and not allow_unsequenced:
                raise SystemExit(
                    "refusing to apply: surrogate tables without a configured sequence "
                    f"{unseq}. Set `sequence:` for each (they allocate keys via NEXTVAL, which keeps "
                    "the sequence ahead of the data so application inserts never collide). Re-run "
                    "with allow_unsequenced=True only for a throwaway/dev target.")

        # Phase A — build id-maps (match + allocate). Required by the counts and the writes, since
        # a child's SQL joins its parents' id-maps. Keys are allocated only when applying.
        for name in order:
            for stmt in sqlgen.build_idmap(plans[name], stg, tgt, with_keys=apply):
                cur.execute(stmt)

        # Phase B — report counts (id-maps now exist).
        print(f"{'TABLE':40} {'matched':>9} {'insert':>9} {'tgt-only':>9}")
        for name in order:
            c = _counts(cur, plans[name], stg, tgt)
            print(f"{name:40} {c.matched:>9} {c.to_insert:>9} {c.target_only:>9}")

        if not apply:
            conn.rollback()                      # discard the id-map population; nothing else touched
            print("\ndry-run only; no changes made.")
            return

        ch = cfg.constraint_handling
        cons = fk_constraints(schema, order)

        # Loosen referential integrity for the writes.
        #   defer   — SET CONSTRAINTS ALL DEFERRED: transactional, validated at COMMIT (needs
        #             DEFERRABLE constraints). The whole run is then atomic.
        #   disable — ALTER ... DISABLE CONSTRAINT: DDL (implicit COMMIT). The writes are one
        #             transaction between the DDL bookends; full-run atomicity relies on the pre-run
        #             backup. Re-enabled WITH VALIDATE so a residual violation aborts.
        #   none    — leave constraints as-is (topological order already satisfies them for a DAG).
        if ch == "defer":
            cur.execute("SET CONSTRAINTS ALL DEFERRED")
        elif ch == "disable":
            for t, c in cons:
                cur.execute(sqlgen.disable_constraint(tgt, t, c))

        # Phase C — writes.
        for name in order:
            for stmt in sqlgen.write_statements(plans[name], stg, tgt):
                cur.execute(stmt)
        # second pass: fix FK columns nulled to break a nullable cycle/self-reference
        for name in order:
            du = sqlgen.deferred_update(plans[name], stg, tgt)
            if du:
                cur.execute(du)

        conn.commit()  # for defer, this is where deferred constraints validate (violation -> error)

        if ch == "disable":
            for t, c in cons:
                cur.execute(sqlgen.enable_constraint(tgt, t, c))  # ENABLE VALIDATE; bad data -> error
        print("\napplied and committed.")
    except Exception:
        conn.rollback()
        # Do not leave constraints disabled after a failed run; re-enable NOVALIDATE (data may be
        # partial) so the schema is left consistent for recovery.
        if apply and cfg.constraint_handling == "disable":
            for t, c in fk_constraints(schema, order):
                try:
                    cur.execute(f"ALTER TABLE {cfg.target_schema}.{t} ENABLE NOVALIDATE CONSTRAINT {c}")
                except Exception:
                    pass
        raise
    finally:
        for name in order:
            if plans[name].needs_idmap:
                try:
                    cur.execute(f"DROP TABLE {sqlgen.idmap_name(cfg.staging_schema, name)}")
                except Exception:
                    pass
        conn.close()
