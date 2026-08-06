"""Generate the SQL a table plan runs. All work is set-based and stays in the database.

Two shapes:
  * Natural / value tables (no surrogate) — a single MERGE keyed on natural identity: update
    matched, insert unmatched, leave target-only rows untouched.
  * Surrogate tables — four steps, because children reference the surrogate and need the
    source->target mapping exposed in an id-map:
      1. match     : record src->tgt for rows found by natural identity (is_new=0)
      2. allocate  : mint fresh target keys for unmatched source rows (is_new=1)
      3. insert    : insert the new rows using the allocated keys
      4. update    : MERGE-update the matched rows

Remapped columns are translated through their origin table's id-map via LEFT JOIN (LEFT so a
NULL FK stays NULL).
"""
from __future__ import annotations

from .plan import TablePlan


def idmap_name(stg: str, table: str) -> str:
    return f"{stg}.IDMAP_{table}"


def _remap_joins(p: TablePlan, stg: str, s_alias: str = "s") -> list[str]:
    joins = []
    for col, origin in sorted(p.remaps.items()):
        a = f"im_{col}"
        joins.append(
            f"LEFT JOIN {idmap_name(stg, origin)} {a} ON {a}.SRC_KEY = {s_alias}.{col}"
        )
    return joins


def _expr(p: TablePlan, col: str, s_alias: str = "s") -> str:
    """Value expression for a column: remapped id-maps translate, everything else passes through.
    Used for MATCHING (identity / hash / delete scope)."""
    if col in p.remaps:
        return f"im_{col}.TGT_KEY"
    return f"{s_alias}.{col}"


def _write_expr(p: TablePlan, col: str, s_alias: str = "s") -> str:
    """Value WRITTEN for a column (insert / update). Audit-override columns get their configured SQL
    expression (e.g. SYSTIMESTAMP); everything else is the source value / remapped id. Never used
    for matching, so overrides can't affect which rows pair up."""
    if col in p.audit_override:
        return p.audit_override[col]
    return _expr(p, col, s_alias)


_HASH_NULL = "~RSNULL~"  # sentinel so NULL and empty-string hash differently


def _hash_of(p: TablePlan, cols: list[str], source_side: bool) -> str:
    """STANDARD_HASH(SHA256) over `cols`, canonicalised. Source side remaps FK columns to target
    surrogates (via id-map) so the hash matches the target side. Determinism relies on the session
    NLS set by the runner (date/timestamp/number formats)."""
    def val(c: str) -> str:
        return _expr(p, c) if source_side else f"d.{c}"
    parts = " || '|' || ".join(f"NVL(TO_CHAR({val(c)}), '{_HASH_NULL}')" for c in cols)
    return f"STANDARD_HASH({parts}, 'SHA256')"


def _match_on(p: TablePlan) -> str:
    """Condition matching a staging row (s / remapped) to a target row (d)."""
    if p.mode == "hash":
        return f"{_hash_of(p, p.hash_cols, False)} = {_hash_of(p, p.hash_cols, True)}"
    return " AND ".join(f"d.{c} = {_expr(p, c)}" for c in p.identity)


def create_idmap(p: TablePlan, stg: str) -> str:
    return (
        f"CREATE TABLE {idmap_name(stg, p.name)} "
        f"(SRC_KEY NUMBER, TGT_KEY NUMBER, IS_NEW NUMBER(1)) NOLOGGING"
    )


def _identity_on(p: TablePlan, d: str = "d", x: str = "x") -> str:
    return " AND ".join(f"{d}.{c} = {x}.{c}" for c in p.identity)


def merge_natural(p: TablePlan, stg: str, tgt: str) -> str:
    """Single MERGE for a table without a surrogate (natural or value identity)."""
    cols = p.columns
    joins = "\n    ".join(_remap_joins(p, stg))
    select = ",\n           ".join(
        (f"NULL AS {c}" if c in p.deferred_cols else f"{_write_expr(p, c)} AS {c}") for c in cols)
    src = f"""(
    SELECT {select}
    FROM {stg}.{p.name} s
    {joins}
  )"""
    non_ident = [c for c in cols if c not in p.identity and c not in p.deferred_cols]
    set_clause = ",\n       ".join(f"d.{c} = x.{c}" for c in non_ident) or "d.{0} = x.{0}".format(cols[0])
    insert_cols = ", ".join(cols)
    insert_vals = ", ".join(f"x.{c}" for c in cols)
    return f"""MERGE INTO {tgt}.{p.name} d
USING {src} x
ON ({_identity_on(p)})
WHEN MATCHED THEN UPDATE SET
       {set_clause}
WHEN NOT MATCHED THEN INSERT ({insert_cols})
VALUES ({insert_vals})"""


def match_surrogate(p: TablePlan, stg: str, tgt: str) -> str:
    joins = "\n    ".join(_remap_joins(p, stg))
    on = " AND ".join(f"d.{c} = {_expr(p, c)}" for c in p.identity)
    return f"""INSERT INTO {idmap_name(stg, p.name)} (SRC_KEY, TGT_KEY, IS_NEW)
SELECT s.{p.surrogate}, d.{p.surrogate}, 0
FROM {stg}.{p.name} s
    {joins}
JOIN {tgt}.{p.name} d ON ({on})"""


def allocate_surrogate(p: TablePlan, stg: str, tgt: str) -> str:
    im = idmap_name(stg, p.name)
    if p.sequence:
        newkey = f"{p.sequence}.NEXTVAL"
        return f"""INSERT INTO {im} (SRC_KEY, TGT_KEY, IS_NEW)
SELECT s.{p.surrogate}, {newkey}, 1
FROM {stg}.{p.name} s
WHERE s.{p.surrogate} NOT IN (SELECT SRC_KEY FROM {im})"""
    # No sequence configured: allocate above the current max. WARNING: does not advance the
    # real sequence — set `sequence:` in the config for production use.
    base = f"(SELECT NVL(MAX({p.surrogate}), 0) FROM {tgt}.{p.name})"
    return f"""INSERT INTO {im} (SRC_KEY, TGT_KEY, IS_NEW)
SELECT s.{p.surrogate}, {base} + ROWNUM, 1
FROM {stg}.{p.name} s
WHERE s.{p.surrogate} NOT IN (SELECT SRC_KEY FROM {im})"""


def insert_surrogate(p: TablePlan, stg: str, tgt: str) -> str:
    im = idmap_name(stg, p.name)
    joins = "\n    ".join(_remap_joins(p, stg))
    exprs = []
    for c in p.columns:
        if c == p.surrogate:
            exprs.append("m.TGT_KEY")
        elif c in p.deferred_cols:
            exprs.append("NULL")                 # set in a second pass (broken nullable cycle)
        else:
            exprs.append(_write_expr(p, c))
    select = ",\n       ".join(f"{e} AS {c}" for e, c in zip(exprs, p.columns))
    cols = ", ".join(p.columns)
    return f"""INSERT INTO {tgt}.{p.name} ({cols})
SELECT {select}
FROM {stg}.{p.name} s
JOIN {im} m ON m.SRC_KEY = s.{p.surrogate} AND m.IS_NEW = 1
    {joins}"""


def update_surrogate(p: TablePlan, stg: str, tgt: str) -> str:
    im = idmap_name(stg, p.name)
    joins = "\n    ".join(_remap_joins(p, stg))
    non_sur = [c for c in p.columns if c != p.surrogate and c not in p.deferred_cols]
    select = ",\n           ".join(f"{_write_expr(p, c)} AS {c}" for c in non_sur)
    set_clause = ",\n       ".join(f"d.{c} = x.{c}" for c in non_sur)
    return f"""MERGE INTO {tgt}.{p.name} d
USING (
    SELECT m.TGT_KEY AS {p.surrogate},
           {select}
    FROM {stg}.{p.name} s
    JOIN {im} m ON m.SRC_KEY = s.{p.surrogate} AND m.IS_NEW = 0
    {joins}
  ) x
ON (d.{p.surrogate} = x.{p.surrogate})
WHEN MATCHED THEN UPDATE SET
       {set_clause}"""


def delete_orphans(p: TablePlan, stg: str, tgt: str) -> str:
    """Delete Target children absent from Source, scoped to Source-matched parents only.

    Scope = rows whose owning surrogate FK (a remapped column) points at a parent present in the
    id-map — i.e. a Source-originated parent. Children under target-only parents are never in the
    id-map, so their subtree is untouched (preserved).
    """
    # Scope by the *owning* FK only: a remapped column that is part of the identity. Incidental
    # surrogate FKs (e.g. a product reference on an order line) must not narrow the delete scope.
    owner = {c: o for c, o in p.remaps.items() if c in p.identity}
    if not owner:
        raise ValueError(
            f"{p.name}: delete_orphans needs an owning surrogate FK in the identity, none found")
    scope = " AND ".join(
        f"d.{col} IN (SELECT TGT_KEY FROM {idmap_name(stg, origin)})"
        for col, origin in sorted(owner.items())
    )
    joins = "\n    ".join(_remap_joins(p, stg))
    on = " AND ".join(f"d.{c} = {_expr(p, c)}" for c in p.identity)
    return f"""DELETE FROM {tgt}.{p.name} d
WHERE {scope}
  AND NOT EXISTS (
    SELECT 1 FROM {stg}.{p.name} s
    {joins}
    WHERE {on})"""


def deferred_update(p: TablePlan, stg: str, tgt: str) -> str | None:
    """Second pass: set the FK columns that were nulled to break a nullable cycle/self-reference.
    Runs after every table is loaded, so both endpoints of the deferred edge exist. Requires the
    table to have its own id-map (its surrogate) to locate the target rows."""
    if not p.deferred_cols:
        return None
    if not p.needs_idmap:
        raise NotImplementedError(
            f"{p.name}: deferred FK fixup needs a surrogate id-map (only self-refs / surrogate "
            "tables are supported)")
    im = idmap_name(stg, p.name)
    joins, sel, setc = [], [], []
    for c in p.deferred_cols:
        a = f"im_{c}"
        joins.append(f"LEFT JOIN {idmap_name(stg, p.remaps[c])} {a} ON {a}.SRC_KEY = s.{c}")
        sel.append(f"{a}.TGT_KEY AS {c}")
        setc.append(f"d.{c} = x.{c}")
    joins_s = "\n    ".join(joins)
    sel_s = ",\n           ".join(sel)
    set_s = ",\n       ".join(setc)
    return f"""MERGE INTO {tgt}.{p.name} d
USING (
    SELECT m.TGT_KEY AS {p.surrogate},
           {sel_s}
    FROM {stg}.{p.name} s
    JOIN {im} m ON m.SRC_KEY = s.{p.surrogate}
    {joins_s}
  ) x
ON (d.{p.surrogate} = x.{p.surrogate})
WHEN MATCHED THEN UPDATE SET
       {set_s}"""


def reload_table(p: TablePlan, stg: str, tgt: str) -> list[str]:
    joins = "\n    ".join(_remap_joins(p, stg))
    select = ",\n       ".join(f"{_write_expr(p, c)} AS {c}" for c in p.columns)
    cols = ", ".join(p.columns)
    return [
        f"TRUNCATE TABLE {tgt}.{p.name}",
        f"INSERT INTO {tgt}.{p.name} ({cols})\nSELECT {select}\nFROM {stg}.{p.name} s\n    {joins}",
    ]


def merge_hash(p: TablePlan, stg: str, tgt: str) -> str:
    """MERGE keyed on a content hash, for wide value rows. Scoped to tables without a surrogate
    and without delete_orphans."""
    if p.surrogate:
        raise NotImplementedError(f"{p.name}: hash mode with a surrogate key not supported; use value mode")
    if p.delete_orphans:
        raise NotImplementedError(f"{p.name}: hash mode with delete_orphans not supported")
    cols = p.columns
    joins = "\n    ".join(_remap_joins(p, stg))
    select = ",\n           ".join(f"{_write_expr(p, c)} AS {c}" for c in cols)
    src_hash = _hash_of(p, p.hash_cols, source_side=True)
    tgt_hash = _hash_of(p, p.hash_cols, source_side=False)
    non_hash = [c for c in cols if c not in p.hash_cols]     # audit columns to refresh on match
    set_clause = ",\n       ".join(f"d.{c} = x.{c}" for c in non_hash) or f"d.{cols[0]} = x.{cols[0]}"
    insert_cols = ", ".join(cols)
    insert_vals = ", ".join(f"x.{c}" for c in cols)
    return f"""MERGE INTO {tgt}.{p.name} d
USING (
    SELECT {select},
           {src_hash} AS RESYNC_HASH
    FROM {stg}.{p.name} s
    {joins}
  ) x
ON ({tgt_hash} = x.RESYNC_HASH)
WHEN MATCHED THEN UPDATE SET
       {set_clause}
WHEN NOT MATCHED THEN INSERT ({insert_cols})
VALUES ({insert_vals})"""


def statements(p: TablePlan, stg: str, tgt: str) -> list[str]:
    """Ordered DML for one table."""
    if p.mode == "reload":
        return reload_table(p, stg, tgt)
    if p.mode == "hash":
        return [merge_hash(p, stg, tgt)]
    if p.needs_idmap:
        stmts = [
            create_idmap(p, stg),
            match_surrogate(p, stg, tgt),
            allocate_surrogate(p, stg, tgt),
            insert_surrogate(p, stg, tgt),
            update_surrogate(p, stg, tgt),
        ]
    else:
        stmts = [merge_natural(p, stg, tgt)]
    if p.delete_orphans:
        stmts.append(delete_orphans(p, stg, tgt))
    return stmts


def dryrun_counts(p: TablePlan, stg: str, tgt: str) -> str:
    """One row: matched / to_insert / target_only, without changing anything."""
    joins = "\n    ".join(_remap_joins(p, stg))
    on = _match_on(p)
    return f"""SELECT
  (SELECT COUNT(*) FROM {stg}.{p.name} s {joins}
     WHERE EXISTS (SELECT 1 FROM {tgt}.{p.name} d WHERE {on}))            AS matched,
  (SELECT COUNT(*) FROM {stg}.{p.name} s {joins}
     WHERE NOT EXISTS (SELECT 1 FROM {tgt}.{p.name} d WHERE {on}))        AS to_insert,
  (SELECT COUNT(*) FROM {tgt}.{p.name} d
     WHERE NOT EXISTS (SELECT 1 FROM {stg}.{p.name} s {joins} WHERE {on})) AS target_only
FROM dual"""


def disable_constraint(tgt: str, table: str, name: str) -> str:
    return f"ALTER TABLE {tgt}.{table} DISABLE CONSTRAINT {name}"


def enable_constraint(tgt: str, table: str, name: str) -> str:
    # ENABLE VALIDATE checks existing rows; a residual violation aborts the statement.
    return f"ALTER TABLE {tgt}.{table} ENABLE VALIDATE CONSTRAINT {name}"
