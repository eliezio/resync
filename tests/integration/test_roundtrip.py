"""End-to-end round-trip against a real Oracle. OPT-IN and SKIPPED unless a database is reachable.

Connection (in order):
  1. env RESYNC_TEST_DSN / RESYNC_TEST_USER / RESYNC_TEST_PASSWORD — a DBA-capable account (it
     creates the SALES and RESYNC_STG schemas, so it needs CREATE USER / CREATE ANY TABLE / DML on
     any schema; e.g. a throwaway test SYSTEM-like user).
  2. testcontainers `gvenzl/oracle-free` if installed and Docker is available.
  3. otherwise the whole module is skipped.

  pip install -e '.[integration]'      # oracledb + testcontainers
  RESYNC_TEST_DSN=host:1521/FREEPDB1 RESYNC_TEST_USER=system RESYNC_TEST_PASSWORD=… pytest tests/integration

NOTE: authored offline; validate on its first real run.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

import pytest

oracledb = pytest.importorskip("oracledb")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema as S  # noqa: E402

from resync_engine import runner  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CATALOG = os.path.join(HERE, "examples", "sample_catalog.json")
CONFIG = os.path.join(HERE, "examples", "sample_resync.yaml")
TGT = "SALES"
STG = "RESYNC_STG"
PWD = "resync_test_pw1"


# --------------------------------------------------------------------------- connection

def _from_container():
    try:
        from testcontainers.oracle import OracleDbContainer
    except Exception:
        pytest.skip("no RESYNC_TEST_* env and testcontainers not installed")
    try:
        c = OracleDbContainer("gvenzl/oracle-free:slim-faststart")
        c.start()
    except Exception as e:  # Docker missing / image pull failed
        pytest.skip(f"cannot start Oracle container: {e}")
    from urllib.parse import urlparse
    u = urlparse(c.get_connection_url().replace("oracle+oracledb", "oracle"))
    dsn = f"{u.hostname}:{u.port}/{u.path.lstrip('/')}"
    return c, dsn, u.username, u.password


@pytest.fixture(scope="module")
def db():
    dsn = os.environ.get("RESYNC_TEST_DSN")
    container = None
    if dsn:
        user = os.environ["RESYNC_TEST_USER"]
        password = os.environ["RESYNC_TEST_PASSWORD"]
    else:
        container, dsn, user, password = _from_container()

    admin = oracledb.connect(user=user, password=password, dsn=dsn)
    admin.autocommit = True
    cur = admin.cursor()
    _drop_schemas(cur)
    for s in (TGT, STG):
        cur.execute(f"CREATE USER {s} IDENTIFIED BY {PWD}")
        cur.execute(f"GRANT UNLIMITED TABLESPACE TO {s}")
    for stmt in S.target_ddl(TGT) + S.staging_ddl(STG):
        cur.execute(stmt)
    try:
        yield admin, dsn, user, password
    finally:
        _drop_schemas(cur)
        admin.close()
        if container is not None:
            container.stop()


def _drop_schemas(cur):
    for s in (TGT, STG):
        try:
            cur.execute(f"DROP USER {s} CASCADE")
        except Exception:
            pass


# --------------------------------------------------------------------------- seed

def _rows(cur, schema, table, cols, rows):
    ph = ", ".join(f":{i+1}" for i in range(len(cols)))
    cur.executemany(f"INSERT INTO {schema}.{table} ({', '.join(cols)}) VALUES ({ph})", rows)


def _seed(admin):
    cur = admin.cursor()
    TS = dt.datetime(2026, 1, 1)
    D = dt.date(2026, 1, 1)

    # --- TARGET: state left by a prior sync + local (target-only) additions ---
    _rows(cur, TGT, "CURRENCY", ["CURRENCY_CD", "CURRENCY_DESC", "UPDATE_ID", "UPDATE_TMSTMP"],
          [("USD", "US Dollar", "PROD", TS), ("EUR", "Euro", "PROD", TS)])
    _rows(cur, TGT, "ORDER_STATUS", ["STATUS_CD", "STATUS_DESC", "UPDATE_ID", "UPDATE_TMSTMP"],
          [("NEW", "New", "PROD", TS), ("SHIP", "Shipped", "PROD", TS)])
    _rows(cur, TGT, "CUSTOMER",
          ["CUSTOMER_ID", "CUSTOMER_CD", "CUSTOMER_NAME", "VERSION_ID", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(100, "ACME", "Acme OLD", 1, "PROD", TS),
           (999, "DEVONLY", "Dev-only customer", 1, "DEV", TS)])         # target-only
    _rows(cur, TGT, "PRODUCT",
          ["PRODUCT_ID", "SKU", "PRODUCT_NAME", "CURRENCY_CD", "VERSION_ID", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(200, "SKU1", "Widget", "USD", 1, "PROD", TS)])
    _rows(cur, TGT, "SALES_ORDER",
          ["ORDER_ID", "ORDER_NO", "CUSTOMER_ID", "STATUS_CD", "CURRENCY_CD", "ORDER_DATE",
           "VERSION_ID", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(300, "SO1", 100, "NEW", "USD", D, 1, "PROD", TS),
           (888, "SODEV", 999, "NEW", "USD", D, 1, "DEV", TS)])           # target-only order
    _rows(cur, TGT, "ORDER_LINE",
          ["ORDER_ID", "LINE_NO", "PRODUCT_ID", "QTY", "UNIT_PRICE", "CURRENCY_CD", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(300, 1, 200, 5, 10, "USD", "PROD", TS),
           (300, 9, 200, 1, 10, "USD", "PROD", TS),                      # orphan: absent from source
           (888, 1, 200, 2, 10, "USD", "DEV", TS)])                      # under target-only order
    _rows(cur, TGT, "ORDER_LABEL", ["ORDER_ID", "LABEL_CD", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(300, "VIP", "PROD", TS), (300, "OLD", "PROD", TS)])          # OLD is an orphan
    _rows(cur, TGT, "ORDER_LINE_ALLOC",
          ["ORDER_ID", "LINE_NO", "ALLOC_SEQ", "WAREHOUSE_CD", "QTY", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(300, 1, 1, "WH1", 5, "PROD", TS)])

    # --- STAGING: current source snapshot (source surrogate keys differ) ---
    _rows(cur, STG, "CURRENCY", ["CURRENCY_CD", "CURRENCY_DESC", "UPDATE_ID", "UPDATE_TMSTMP"],
          [("USD", "US Dollar", "PROD", TS), ("EUR", "Euro EDITED", "PROD", TS)])
    _rows(cur, STG, "ORDER_STATUS", ["STATUS_CD", "STATUS_DESC", "UPDATE_ID", "UPDATE_TMSTMP"],
          [("NEW", "New", "PROD", TS), ("SHIP", "Shipped", "PROD", TS)])
    _rows(cur, STG, "CUSTOMER",
          ["CUSTOMER_ID", "CUSTOMER_CD", "CUSTOMER_NAME", "VERSION_ID", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(1, "ACME", "Acme NEW", 2, "PROD", TS)])                      # edit; matches ACME by CD
    _rows(cur, STG, "PRODUCT",
          ["PRODUCT_ID", "SKU", "PRODUCT_NAME", "CURRENCY_CD", "VERSION_ID", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(2, "SKU1", "Widget", "USD", 2, "PROD", TS)])
    _rows(cur, STG, "SALES_ORDER",
          ["ORDER_ID", "ORDER_NO", "CUSTOMER_ID", "STATUS_CD", "CURRENCY_CD", "ORDER_DATE",
           "VERSION_ID", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(3, "SO1", 1, "SHIP", "USD", D, 2, "PROD", TS),               # status edit
           (4, "SO2", 1, "NEW", "USD", D, 1, "PROD", TS)])               # new order
    _rows(cur, STG, "ORDER_LINE",
          ["ORDER_ID", "LINE_NO", "PRODUCT_ID", "QTY", "UNIT_PRICE", "CURRENCY_CD", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(3, 1, 2, 7, 10, "USD", "PROD", TS),                          # qty edit; no line 9
           (4, 1, 2, 3, 10, "USD", "PROD", TS)])
    _rows(cur, STG, "ORDER_LABEL", ["ORDER_ID", "LABEL_CD", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(3, "VIP", "PROD", TS)])                                      # no OLD
    _rows(cur, STG, "ORDER_LINE_ALLOC",
          ["ORDER_ID", "LINE_NO", "ALLOC_SEQ", "WAREHOUSE_CD", "QTY", "UPDATE_ID", "UPDATE_TMSTMP"],
          [(3, 1, 1, "WH1", 7, "PROD", TS)])
    admin.commit()


def _scalar(admin, sql):
    cur = admin.cursor()
    cur.execute(sql)
    return cur.fetchone()[0]


# --------------------------------------------------------------------------- the test

def test_roundtrip(db):
    admin, dsn, user, password = db
    _seed(admin)

    runner.run(CATALOG, CONFIG, dsn=dsn, user=user, password=password, apply=True)

    # 1. target-only data preserved
    assert _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.CUSTOMER WHERE CUSTOMER_CD='DEVONLY'") == 1
    assert _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.SALES_ORDER WHERE ORDER_NO='SODEV'") == 1
    assert _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.ORDER_LINE ol JOIN {TGT}.SALES_ORDER o "
                          f"ON o.ORDER_ID=ol.ORDER_ID WHERE o.ORDER_NO='SODEV'") == 1

    # 2. source edits applied to matched rows
    assert _scalar(admin, f"SELECT CUSTOMER_NAME FROM {TGT}.CUSTOMER WHERE CUSTOMER_CD='ACME'") == "Acme NEW"
    assert _scalar(admin, f"SELECT STATUS_CD FROM {TGT}.SALES_ORDER WHERE ORDER_NO='SO1'") == "SHIP"
    assert _scalar(admin, f"SELECT QTY FROM {TGT}.ORDER_LINE ol JOIN {TGT}.SALES_ORDER o "
                          f"ON o.ORDER_ID=ol.ORDER_ID WHERE o.ORDER_NO='SO1' AND ol.LINE_NO=1") == 7

    # 3. new source row inserted with a fresh target key from the sequence (not the source id 4)
    new_id = _scalar(admin, f"SELECT ORDER_ID FROM {TGT}.SALES_ORDER WHERE ORDER_NO='SO2'")
    assert new_id >= 1000

    # 4. owned-child orphans deleted within the matched parent, but not under target-only parents
    so1 = _scalar(admin, f"SELECT ORDER_ID FROM {TGT}.SALES_ORDER WHERE ORDER_NO='SO1'")
    assert _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.ORDER_LINE WHERE ORDER_ID={so1} AND LINE_NO=9") == 0
    assert _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.ORDER_LABEL WHERE ORDER_ID={so1} AND LABEL_CD='OLD'") == 0
    assert _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.ORDER_LABEL WHERE ORDER_ID={so1} AND LABEL_CD='VIP'") == 1

    # 5. referential integrity holds (constraints re-enabled WITH VALIDATE without error)
    bad = _scalar(admin,
                  f"SELECT COUNT(*) FROM {TGT}.ORDER_LINE ol "
                  f"WHERE NOT EXISTS (SELECT 1 FROM {TGT}.SALES_ORDER o WHERE o.ORDER_ID=ol.ORDER_ID)")
    assert bad == 0

    # 6. idempotent: a second apply inserts/deletes nothing
    orders_before = _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.SALES_ORDER")
    lines_before = _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.ORDER_LINE")
    runner.run(CATALOG, CONFIG, dsn=dsn, user=user, password=password, apply=True)
    assert _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.SALES_ORDER") == orders_before
    assert _scalar(admin, f"SELECT COUNT(*) FROM {TGT}.ORDER_LINE") == lines_before
