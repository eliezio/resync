"""Per-table execution plan: classify surrogate vs natural identity, and resolve which columns
carry a surrogate value that must be remapped through a parent id-map.

The key subtlety is *surrogate lineage*: a surrogate value propagates down through composite
keys. `ORDER_ID` originates in SALES_ORDER (a surrogate PK) and reappears as an FK column in
ORDER_LINE and again in ORDER_LINE_ALLOC. Every occurrence must be remapped through the
*order* id-map, even where the local FK points at an intermediate table (ORDER_LINE). We
propagate the origin in topological order.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .graph import load_order
from .model import Config, Schema


@dataclass
class TablePlan:
    name: str
    mode: str
    columns: list[str]                 # insertable columns (hidden removed)
    identity: list[str]
    pk: list[str]
    surrogate: str | None              # single surrogate PK column, if any
    sequence: str | None
    delete_orphans: bool = False       # owned child: delete Target children absent from Source
    # column -> origin table whose id-map remaps it (surrogate lineage)
    remaps: dict[str, str] = field(default_factory=dict)
    # FK columns nulled on insert and set in a second pass (broken nullable cycle / self-ref)
    deferred_cols: list[str] = field(default_factory=list)
    # columns hashed to form identity (hash mode): data columns minus surrogate/audit/hash_exclude
    hash_cols: list[str] = field(default_factory=list)
    # column -> SQL expression written instead of the source value (audit override)
    audit_override: dict[str, str] = field(default_factory=dict)
    # columns that look like a surrogate reference but have no resolvable id-map (blocked)
    unresolved: list[str] = field(default_factory=list)

    @property
    def needs_idmap(self) -> bool:
        return self.surrogate is not None


def _surrogate_of(schema: Schema, cfg_ident: list[str], table_name: str) -> str | None:
    """A single-column PK that is not part of the declared natural identity is the surrogate."""
    pk = schema.tables[table_name].pk
    if len(pk) == 1 and pk[0] not in cfg_ident:
        return pk[0]
    return None


def build_plans(schema: Schema, config: Config) -> tuple[list[str], dict[str, TablePlan]]:
    """Return (parents-first order, plans) for all in-scope tables (mode != out_of_scope)."""
    scope = {n for n, c in config.tables.items() if c.mode != "out_of_scope"}
    extra_parents: dict[str, set[str]] = {}
    for name, tc in config.tables.items():
        if tc.mode == "out_of_scope":
            continue
        for mf in tc.manual_fks:
            extra_parents.setdefault(name, set()).add(mf["parent"])
    deferred_edges: list = []
    order = load_order(schema, scope, extra_parents, deferred_edges)
    deferred_cols: dict[str, list[str]] = {}
    for child, fk in deferred_edges:
        for ccol, _pcol in fk.pairs:
            deferred_cols.setdefault(child, []).append(ccol)

    # origin[(table, column)] = table whose surrogate id-map remaps this column value.
    origin: dict[tuple[str, str], str] = {}
    plans: dict[str, TablePlan] = {}

    for name in order:
        tcfg = config.tables[name]
        tbl = schema.tables[name]
        surrogate = _surrogate_of(schema, tcfg.identity, name)
        if surrogate is not None:
            origin[(name, surrogate)] = name  # a surrogate is its own origin

        remaps: dict[str, str] = {}
        unresolved: list[str] = []

        # Enforced FKs + any manually declared logical FKs.
        fk_specs = [(fk.parent, fk.pairs) for fk in tbl.fks]
        for mf in tcfg.manual_fks:
            fk_specs.append((mf["parent"], [tuple(p.split("->")) for p in mf["columns"]]))

        for parent, pairs in fk_specs:
            for child_col, parent_col in pairs:
                key = (parent, parent_col)
                if key in origin:                      # parent column carries a surrogate
                    remaps[child_col] = origin[key]
                    origin[(name, child_col)] = origin[key]  # propagate lineage downward

        # A declared-identity column that references an out-of-scope surrogate we cannot remap
        # is a blocker (an *_ID identity column with no resolvable id-map).
        for col in tcfg.identity:
            if col.endswith("_ID") and col != surrogate and col not in remaps:
                # Heuristic: an *_ID identity column with no id-map is suspicious.
                unresolved.append(col)

        hash_cols: list[str] = []
        if tcfg.mode == "hash":
            excl = set(config.audit_exclude) | set(tcfg.hash_exclude)
            if surrogate:
                excl.add(surrogate)
            hash_cols = [c for c in tbl.data_columns if c not in excl]

        overrides = {c: e for c, e in config.audit_override.items() if c in tbl.data_columns}

        plans[name] = TablePlan(
            name=name, mode=tcfg.mode, columns=tbl.data_columns,
            identity=tcfg.identity, pk=tbl.pk, surrogate=surrogate,
            sequence=tcfg.sequence, delete_orphans=tcfg.delete_orphans,
            remaps=remaps, unresolved=unresolved,
            deferred_cols=deferred_cols.get(name, []),
            hash_cols=hash_cols,
            audit_override=overrides,
        )
    return order, plans


def fk_constraints(schema: Schema, order: list[str]) -> list[tuple[str, str]]:
    """(table, constraint_name) for every *enforced* FK on the in-scope tables, in load order.
    Manual (unenforced) FKs are excluded — they have no database constraint to disable/enable."""
    return [(t, fk.name) for t in order for fk in schema.tables[t].fks]


def unsequenced_surrogates(order: list[str], plans: dict[str, "TablePlan"]) -> list[str]:
    """Surrogate tables with no `sequence:` configured. On apply these fall back to MAX+ROWNUM
    allocation, which does not advance the real sequence and risks colliding with future
    application inserts — unsafe for production."""
    return [n for n in order if plans[n].surrogate and not plans[n].sequence]
