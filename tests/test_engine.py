"""Offline tests — no database. Run against the synthetic sample schema in examples/ so the
public repo has no proprietary identifiers. Exercises graph, plan and SQL generation.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resync_engine import sqlgen  # noqa: E402
from resync_engine.graph import CycleError, load_order  # noqa: E402
from resync_engine.model import Config, Schema  # noqa: E402
from resync_engine.plan import build_plans  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(ROOT, "examples", "sample_catalog.json")
CONFIG = os.path.join(ROOT, "examples", "sample_resync.yaml")


def _plans():
    schema = Schema.from_catalog(CATALOG)
    config = Config.from_yaml(CONFIG)
    order, plans = build_plans(schema, config)
    return schema, config, order, plans


def test_acyclic_and_scope():
    _, _, order, _ = _plans()
    assert len(order) == 8, order


def test_parents_before_children():
    schema, _, order, _ = _plans()
    pos = {n: i for i, n in enumerate(order)}
    for t in order:
        for fk in schema.tables[t].fks:
            if fk.parent in pos and fk.parent != t:
                assert pos[fk.parent] < pos[t], f"{fk.parent} must precede {t}"


def test_surrogate_detection():
    _, _, _, plans = _plans()
    assert plans["SALES_ORDER"].surrogate == "ORDER_ID"
    assert plans["CUSTOMER"].surrogate == "CUSTOMER_ID"
    assert plans["PRODUCT"].surrogate == "PRODUCT_ID"
    assert plans["CURRENCY"].surrogate is None          # code PK
    assert plans["ORDER_LINE"].surrogate is None         # composite natural PK


def test_surrogate_lineage_remap():
    _, _, _, plans = _plans()
    # ORDER_ID inside ORDER_LINE_ALLOC remaps through the SALES_ORDER id-map even though the
    # local FK points at ORDER_LINE (which has no surrogate).
    alloc = plans["ORDER_LINE_ALLOC"]
    assert alloc.remaps.get("ORDER_ID") == "SALES_ORDER"
    # ORDER_LINE's product reference remaps via PRODUCT.
    assert plans["ORDER_LINE"].remaps.get("PRODUCT_ID") == "PRODUCT"


def test_sql_generates_for_every_table():
    _, cfg, order, plans = _plans()
    for name in order:
        stmts = sqlgen.statements(plans[name], cfg.staging_schema, cfg.target_schema)
        assert stmts and all(isinstance(s, str) and s.strip() for s in stmts)


def test_natural_table_single_merge():
    _, cfg, _, plans = _plans()
    stmts = sqlgen.statements(plans["CURRENCY"], cfg.staging_schema, cfg.target_schema)
    assert len(stmts) == 1 and stmts[0].startswith("MERGE INTO")


def test_surrogate_table_four_steps():
    _, cfg, _, plans = _plans()
    stmts = sqlgen.statements(plans["SALES_ORDER"], cfg.staging_schema, cfg.target_schema)
    assert len(stmts) == 5  # create idmap + match + allocate + insert + update
    assert stmts[0].startswith("CREATE TABLE")


def test_owned_child_delete_scoped_to_owner_only():
    _, cfg, _, plans = _plans()
    stmts = sqlgen.statements(plans["ORDER_LINE"], cfg.staging_schema, cfg.target_schema)
    dels = [s for s in stmts if s.startswith("DELETE")]
    assert len(dels) == 1
    d = dels[0]
    # scoped to the owning order id-map...
    assert "d.ORDER_ID IN (SELECT TGT_KEY FROM RESYNC_STG.IDMAP_SALES_ORDER)" in d
    # ...but NOT narrowed by the incidental product FK
    assert "IDMAP_PRODUCT)" not in d.split("NOT EXISTS")[0]
    assert "NOT EXISTS" in d


def test_lineage_owned_grandchild_scopes_to_root():
    _, cfg, _, plans = _plans()
    stmts = sqlgen.statements(plans["ORDER_LINE_ALLOC"], cfg.staging_schema, cfg.target_schema)
    d = next(s for s in stmts if s.startswith("DELETE"))
    assert "IDMAP_SALES_ORDER" in d  # scope resolves through lineage to the root


def test_reference_and_roots_never_delete():
    _, cfg, _, plans = _plans()
    for name in ("CURRENCY", "ORDER_STATUS", "CUSTOMER", "PRODUCT", "SALES_ORDER"):
        stmts = sqlgen.statements(plans[name], cfg.staging_schema, cfg.target_schema)
        assert not any(s.startswith("DELETE") for s in stmts), f"{name} must not delete"


def test_cycle_detection():
    from resync_engine.model import Column, ForeignKey, Table
    a = Table("A", [Column("ID", False, True)], ["ID"], [ForeignKey("fk", "B", True, [("BID", "ID")])])
    b = Table("B", [Column("ID", False, True)], ["ID"], [ForeignKey("fk", "A", True, [("AID", "ID")])])
    schema = Schema({"A": a, "B": b})
    try:
        load_order(schema, {"A", "B"})
        assert False, "expected CycleError"
    except CycleError:
        pass


def test_manual_fk_orders_and_remaps():
    """An unenforced FK declared via manual_fks must constrain load order and resolve remap,
    so a table referencing an otherwise-isolated parent is not left BLOCKED."""
    from resync_engine.model import Column, Config as Cfg, Schema as Sch, Table, TableConfig
    ref = Table("REF", [Column("REF_ID", False, True), Column("REF_CD", False, False)],
                ["REF_ID"], [])
    child = Table("CHILD", [Column("CHILD_ID", False, True), Column("REF_ID", False, False),
                            Column("NAME", False, False)], ["CHILD_ID"], [])
    schema = Sch({"REF": ref, "CHILD": child})
    cfg = Cfg(
        tables={
            "REF": TableConfig(mode="natural", identity=["REF_CD"]),
            "CHILD": TableConfig(mode="value", identity=["REF_ID", "NAME"],
                                 manual_fks=[{"parent": "REF", "columns": ["REF_ID->REF_ID"]}]),
        },
        audit_exclude=[], staging_schema="STG", target_schema="TGT")
    order, plans = build_plans(schema, cfg)
    assert order.index("REF") < order.index("CHILD"), order   # manual edge fixes the order
    assert plans["CHILD"].remaps.get("REF_ID") == "REF"       # remap resolves via manual FK
    assert plans["CHILD"].unresolved == []                    # no longer BLOCKED


def test_fk_constraints_listed_in_order():
    from resync_engine.plan import fk_constraints
    schema, _, order, _ = _plans()
    cons = fk_constraints(schema, order)
    assert ("PRODUCT", "PRODUCT_CURRENCY_FK") in cons
    assert ("ORDER_LINE", "LINE_ORDER_FK") in cons
    # only enforced FKs — count equals the catalog edge count (9 in the sample)
    assert len(cons) == 9


def test_constraint_sql_shapes():
    d = sqlgen.disable_constraint("SALES", "PRODUCT", "PRODUCT_CURRENCY_FK")
    e = sqlgen.enable_constraint("SALES", "PRODUCT", "PRODUCT_CURRENCY_FK")
    assert d == "ALTER TABLE SALES.PRODUCT DISABLE CONSTRAINT PRODUCT_CURRENCY_FK"
    assert e == "ALTER TABLE SALES.PRODUCT ENABLE VALIDATE CONSTRAINT PRODUCT_CURRENCY_FK"


def test_constraint_handling_default_is_disable():
    _, cfg, _, _ = _plans()
    assert cfg.constraint_handling == "disable"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
