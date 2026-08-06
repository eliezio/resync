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
    a = Table("A", [Column("ID", False, True)], ["ID"], [ForeignKey("fk", "B", False, [("BID", "ID")])])
    b = Table("B", [Column("ID", False, True)], ["ID"], [ForeignKey("fk", "A", False, [("AID", "ID")])])
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


def test_unsequenced_surrogates_flagged():
    from resync_engine.plan import unsequenced_surrogates
    _, _, order, plans = _plans()
    assert unsequenced_surrogates(order, plans) == []      # sample is fully sequenced
    plans["SALES_ORDER"].sequence = None                    # drop one
    assert unsequenced_surrogates(order, plans) == ["SALES_ORDER"]


def test_nullable_mutual_cycle_breaks():
    from resync_engine.model import Column, ForeignKey, Table
    a = Table("A", [Column("ID", False, True), Column("BID", True, False)], ["ID"],
              [ForeignKey("A_B_FK", "B", True, [("BID", "ID")])])    # nullable -> breakable
    b = Table("B", [Column("ID", False, True), Column("AID", False, False)], ["ID"],
              [ForeignKey("B_A_FK", "A", False, [("AID", "ID")])])   # non-nullable
    schema = Schema({"A": a, "B": b})
    deferred: list = []
    order = load_order(schema, {"A", "B"}, None, deferred)
    assert set(order) == {"A", "B"} and len(order) == 2
    assert deferred and deferred[0][0] == "A"   # the nullable A->B edge was broken


def test_nullable_self_ref_deferred():
    from resync_engine.model import (Column, Config as Cfg, ForeignKey, Schema as Sch, Table,
                                     TableConfig)
    emp = Table("EMP",
                [Column("EMP_ID", False, True), Column("EMP_CD", False, False),
                 Column("EMP_NAME", False, False), Column("MANAGER_ID", True, False)],
                ["EMP_ID"],
                [ForeignKey("EMP_MGR_FK", "EMP", True, [("MANAGER_ID", "EMP_ID")])])
    schema = Sch({"EMP": emp})
    cfg = Cfg(tables={"EMP": TableConfig(mode="natural", identity=["EMP_CD"], sequence="EMP_SEQ")},
              audit_exclude=[], staging_schema="STG", target_schema="TGT")
    order, plans = build_plans(schema, cfg)
    assert order == ["EMP"]                       # self-loop did not block ordering
    p = plans["EMP"]
    assert p.deferred_cols == ["MANAGER_ID"]
    ins = next(x for x in sqlgen.statements(p, "STG", "TGT") if x.startswith("INSERT INTO TGT.EMP"))
    assert "NULL AS MANAGER_ID" in ins            # nulled on insert
    du = sqlgen.deferred_update(p, "STG", "TGT")
    assert du and du.startswith("MERGE INTO TGT.EMP") and "MANAGER_ID" in du and "IDMAP_EMP" in du


def test_non_nullable_self_ref_raises():
    from resync_engine.model import Column, ForeignKey, Table
    t = Table("N", [Column("ID", False, True), Column("PARENT_ID", False, False)], ["ID"],
              [ForeignKey("N_SELF_FK", "N", False, [("PARENT_ID", "ID")])])
    schema = Schema({"N": t})
    try:
        load_order(schema, {"N"})
        assert False, "expected CycleError"
    except CycleError:
        pass


def test_hash_mode_merge():
    from resync_engine.model import (Column, Config as Cfg, ForeignKey, Schema as Sch, Table,
                                     TableConfig)
    ordt = Table("ORD", [Column("ORD_ID", False, True), Column("ORD_NO", False, False)],
                 ["ORD_ID"], [])
    addr = Table("ADDR",
                 [Column("ORD_ID", False, True), Column("ADDR_TYPE", False, True),
                  Column("LINE1", False, False), Column("CITY", False, False),
                  Column("UPDATE_ID", False, False)],
                 ["ORD_ID", "ADDR_TYPE"],
                 [ForeignKey("ADDR_ORD_FK", "ORD", False, [("ORD_ID", "ORD_ID")])])
    schema = Sch({"ORD": ordt, "ADDR": addr})
    cfg = Cfg(tables={
        "ORD": TableConfig(mode="natural", identity=["ORD_NO"], sequence="ORD_SEQ"),
        "ADDR": TableConfig(mode="hash"),
    }, audit_exclude=["UPDATE_ID"], staging_schema="STG", target_schema="TGT")
    _order, plans = build_plans(schema, cfg)
    p = plans["ADDR"]
    assert p.surrogate is None
    assert "UPDATE_ID" not in p.hash_cols                # audit excluded from the hash
    assert {"ORD_ID", "ADDR_TYPE", "LINE1", "CITY"} <= set(p.hash_cols)
    stmt = sqlgen.statements(p, "STG", "TGT")[0]
    assert stmt.startswith("MERGE INTO TGT.ADDR")
    assert "STANDARD_HASH" in stmt and "'SHA256'" in stmt
    assert "im_ORD_ID.TGT_KEY" in stmt                   # FK remapped inside the source-side hash
    assert "d.UPDATE_ID = x.UPDATE_ID" in stmt           # audit refreshed on match


def test_hash_mode_rejects_surrogate():
    from resync_engine.model import (Column, Config as Cfg, Schema as Sch, Table, TableConfig)
    t = Table("H", [Column("H_ID", False, True), Column("V", False, False)], ["H_ID"], [])
    cfg = Cfg(tables={"H": TableConfig(mode="hash")},
              audit_exclude=[], staging_schema="STG", target_schema="TGT")
    _order, plans = build_plans(Sch({"H": t}), cfg)
    try:
        sqlgen.statements(plans["H"], "STG", "TGT")
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass


def test_seed_config_classifies_sample():
    import yaml
    from resync_engine import seed
    d = yaml.safe_load(seed.seed_config(CATALOG, target="SALES"))
    t = d["tables"]
    assert t["CURRENCY"]["mode"] == "natural" and t["CURRENCY"]["identity"] == ["CURRENCY_CD"]
    assert t["CUSTOMER"]["mode"] == "natural" and t["CUSTOMER"]["identity"] == ["CUSTOMER_CD"]
    assert t["SALES_ORDER"]["identity"] == ["ORDER_NO"]
    assert t["ORDER_LINE"]["mode"] == "value"
    assert set(t["ORDER_LINE"]["identity"]) == {"ORDER_ID", "LINE_NO"}
    assert t["ORDER_LINE"].get("delete_orphans") is True
    assert "UPDATE_ID" in d["audit_exclude"] and "VERSION_ID" in d["audit_exclude"]


def test_seed_config_merge_skips_existing():
    from resync_engine import seed
    import yaml
    d = yaml.safe_load(seed.seed_config(CATALOG, existing_path=CONFIG))  # sample_resync.yaml
    # every sample table is already in the config, so a merge seeds nothing
    assert (d.get("tables") or {}) == {} or d.get("tables") is None


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
