"""Typed DDL + seed helpers for the Order/OrderLine sample, used by the integration test.

The sample catalog (examples/sample_catalog.json) carries only column names/PKs/FKs — no types —
so the concrete DDL lives here. Table and FK constraint names match the catalog edges so the
engine's constraint disable/re-enable targets them correctly.
"""
from __future__ import annotations

# column definitions (target schema); staging reuses the columns without constraints
COLUMNS: dict[str, str] = {
    "CURRENCY": "CURRENCY_CD VARCHAR2(3) NOT NULL, CURRENCY_DESC VARCHAR2(100), "
                "UPDATE_ID VARCHAR2(30), UPDATE_TMSTMP TIMESTAMP",
    "ORDER_STATUS": "STATUS_CD VARCHAR2(10) NOT NULL, STATUS_DESC VARCHAR2(100), "
                    "UPDATE_ID VARCHAR2(30), UPDATE_TMSTMP TIMESTAMP",
    "CUSTOMER": "CUSTOMER_ID NUMBER NOT NULL, CUSTOMER_CD VARCHAR2(20) NOT NULL, "
                "CUSTOMER_NAME VARCHAR2(100), VERSION_ID NUMBER, "
                "UPDATE_ID VARCHAR2(30), UPDATE_TMSTMP TIMESTAMP",
    "PRODUCT": "PRODUCT_ID NUMBER NOT NULL, SKU VARCHAR2(20) NOT NULL, PRODUCT_NAME VARCHAR2(100), "
               "CURRENCY_CD VARCHAR2(3), VERSION_ID NUMBER, "
               "UPDATE_ID VARCHAR2(30), UPDATE_TMSTMP TIMESTAMP",
    "SALES_ORDER": "ORDER_ID NUMBER NOT NULL, ORDER_NO VARCHAR2(20) NOT NULL, CUSTOMER_ID NUMBER, "
                   "STATUS_CD VARCHAR2(10), CURRENCY_CD VARCHAR2(3), ORDER_DATE DATE, "
                   "VERSION_ID NUMBER, UPDATE_ID VARCHAR2(30), UPDATE_TMSTMP TIMESTAMP",
    "ORDER_LINE": "ORDER_ID NUMBER NOT NULL, LINE_NO NUMBER NOT NULL, PRODUCT_ID NUMBER, "
                  "QTY NUMBER, UNIT_PRICE NUMBER, CURRENCY_CD VARCHAR2(3), "
                  "UPDATE_ID VARCHAR2(30), UPDATE_TMSTMP TIMESTAMP",
    "ORDER_LABEL": "ORDER_ID NUMBER NOT NULL, LABEL_CD VARCHAR2(20) NOT NULL, "
                   "UPDATE_ID VARCHAR2(30), UPDATE_TMSTMP TIMESTAMP",
    "ORDER_LINE_ALLOC": "ORDER_ID NUMBER NOT NULL, LINE_NO NUMBER NOT NULL, ALLOC_SEQ NUMBER NOT NULL, "
                        "WAREHOUSE_CD VARCHAR2(20), QTY NUMBER, "
                        "UPDATE_ID VARCHAR2(30), UPDATE_TMSTMP TIMESTAMP",
}

PK: dict[str, str] = {
    "CURRENCY": "CURRENCY_CD",
    "ORDER_STATUS": "STATUS_CD",
    "CUSTOMER": "CUSTOMER_ID",
    "PRODUCT": "PRODUCT_ID",
    "SALES_ORDER": "ORDER_ID",
    "ORDER_LINE": "ORDER_ID, LINE_NO",
    "ORDER_LABEL": "ORDER_ID, LABEL_CD",
    "ORDER_LINE_ALLOC": "ORDER_ID, LINE_NO, ALLOC_SEQ",
}

UNIQUE: dict[str, str] = {
    "CUSTOMER": "CUSTOMER_CD",
    "PRODUCT": "SKU",
    "SALES_ORDER": "ORDER_NO",
}

# (fk_name, local_cols, parent_table, parent_cols) — names match examples/sample_catalog.json
FKS: dict[str, list[tuple[str, str, str, str]]] = {
    "PRODUCT": [("PRODUCT_CURRENCY_FK", "CURRENCY_CD", "CURRENCY", "CURRENCY_CD")],
    "SALES_ORDER": [
        ("ORDER_CUSTOMER_FK", "CUSTOMER_ID", "CUSTOMER", "CUSTOMER_ID"),
        ("ORDER_STATUS_FK", "STATUS_CD", "ORDER_STATUS", "STATUS_CD"),
        ("ORDER_CURRENCY_FK", "CURRENCY_CD", "CURRENCY", "CURRENCY_CD"),
    ],
    "ORDER_LINE": [
        ("LINE_ORDER_FK", "ORDER_ID", "SALES_ORDER", "ORDER_ID"),
        ("LINE_PRODUCT_FK", "PRODUCT_ID", "PRODUCT", "PRODUCT_ID"),
        ("LINE_CURRENCY_FK", "CURRENCY_CD", "CURRENCY", "CURRENCY_CD"),
    ],
    "ORDER_LABEL": [("LABEL_ORDER_FK", "ORDER_ID", "SALES_ORDER", "ORDER_ID")],
    "ORDER_LINE_ALLOC": [("ALLOC_LINE_FK", "ORDER_ID, LINE_NO", "ORDER_LINE", "ORDER_ID, LINE_NO")],
}

SEQUENCES = ["CUSTOMER_SEQ", "PRODUCT_SEQ", "ORDER_SEQ"]

# load order (parents first) — same as the engine computes for the sample
ORDER = ["CURRENCY", "ORDER_STATUS", "CUSTOMER", "PRODUCT", "SALES_ORDER",
         "ORDER_LINE", "ORDER_LABEL", "ORDER_LINE_ALLOC"]


def target_ddl(schema: str) -> list[str]:
    """CREATE statements for the live target schema: typed tables, PK/unique/FK, and sequences."""
    stmts: list[str] = []
    for t in ORDER:
        cons = [f"CONSTRAINT {t}_PK PRIMARY KEY ({PK[t]})"]
        if t in UNIQUE:
            cons.append(f"CONSTRAINT {t}_UK UNIQUE ({UNIQUE[t]})")
        for name, lcol, ptab, pcol in FKS.get(t, []):
            cons.append(f"CONSTRAINT {name} FOREIGN KEY ({lcol}) REFERENCES {schema}.{ptab} ({pcol})")
        stmts.append(f"CREATE TABLE {schema}.{t} ({COLUMNS[t]}, {', '.join(cons)})")
    for seq in SEQUENCES:
        stmts.append(f"CREATE SEQUENCE {schema}.{seq} START WITH 1000")
    return stmts


def staging_ddl(schema: str) -> list[str]:
    """CREATE statements for the staging schema: same columns, no constraints (raw source rows)."""
    return [f"CREATE TABLE {schema}.{t} ({COLUMNS[t]})" for t in ORDER]
