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
    return "\n".join(out)


def _fetch_counts(cur, p: TablePlan, cfg: Config) -> Counts:
    cur.execute(sqlgen.dryrun_counts(p, cfg.staging_schema, cfg.target_schema))
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
    try:
        print(f"{'TABLE':40} {'matched':>9} {'insert':>9} {'tgt-only':>9}")
        for name in order:
            c = _fetch_counts(cur, plans[name], cfg)
            print(f"{name:40} {c.matched:>9} {c.to_insert:>9} {c.target_only:>9}")

        if not apply:
            print("\ndry-run only; no changes made.")
            return

        unseq = unsequenced_surrogates(order, plans)
        if unseq and not allow_unsequenced:
            raise SystemExit(
                "refusing to apply: surrogate tables without a configured sequence "
                f"{unseq}. Set `sequence:` for each (they allocate keys via NEXTVAL, which keeps "
                "the sequence ahead of the data so application inserts never collide). Re-run with "
                "allow_unsequenced=True only for a throwaway/dev target.")

        tgt = cfg.target_schema
        ch = cfg.constraint_handling
        cons = fk_constraints(schema, order)

        # Loosen referential integrity for the load.
        #   defer   — SET CONSTRAINTS ALL DEFERRED: transactional, validated at COMMIT (requires
        #             the FK constraints to be DEFERRABLE). The whole run is then atomic.
        #   disable — ALTER ... DISABLE CONSTRAINT: DDL, so each is an implicit COMMIT. The merge
        #             itself is one transaction between the DDL bookends; full-run atomicity relies
        #             on the pre-run target backup (see the runbook). Re-enabled WITH VALIDATE so a
        #             residual violation aborts.
        #   none    — leave constraints as-is (topological order already satisfies them for a DAG).
        if ch == "defer":
            cur.execute("SET CONSTRAINTS ALL DEFERRED")
        elif ch == "disable":
            for t, c in cons:
                cur.execute(sqlgen.disable_constraint(tgt, t, c))

        # --- the merge: one transaction ---
        for name in order:
            for stmt in sqlgen.statements(plans[name], cfg.staging_schema, tgt):
                cur.execute(stmt)

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
