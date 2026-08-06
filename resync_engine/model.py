"""Schema and config loading for the re-sync engine.

Two inputs:
  * catalog  — config.skeleton.json produced by extract.jq (columns, PKs, FK edges).
  * config   — resync.yaml, the reviewed identity config.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

HIDDEN_PREFIX = "SYS_NC"  # Oracle function-based-index virtual columns; never insert/select.


@dataclass
class Column:
    name: str
    nullable: bool
    in_pk: bool


@dataclass
class ForeignKey:
    name: str
    parent: str
    nullable: bool
    pairs: list[tuple[str, str]]  # (child_column, parent_column)


@dataclass
class Table:
    name: str
    columns: list[Column]
    pk: list[str]
    fks: list[ForeignKey] = field(default_factory=list)

    @property
    def data_columns(self) -> list[str]:
        """Real, insertable columns (hidden virtual columns removed)."""
        return [c.name for c in self.columns if not c.name.startswith(HIDDEN_PREFIX)]

    def is_nullable(self, col: str) -> bool:
        for c in self.columns:
            if c.name == col:
                return c.nullable
        return True


@dataclass
class Schema:
    tables: dict[str, Table]

    @classmethod
    def from_catalog(cls, path: str) -> "Schema":
        with open(path) as fh:
            doc = json.load(fh)

        # Group FK edges (skeleton stores them flat, columns as "child->parent" strings).
        edges: dict[str, list[ForeignKey]] = {}
        for e in doc["edges"]:
            pairs = [tuple(p.split("->")) for p in e["columns"]]
            edges.setdefault(e["child"], []).append(
                ForeignKey(name=e["fk"], parent=e["parent"],
                           nullable=bool(e["nullable"]), pairs=pairs)
            )

        tables = {}
        for t in doc["tables"]:
            cols = [Column(c["name"], bool(c["nullable"]), bool(c["in_pk"]))
                    for c in t["columns"]]
            tables[t["name"]] = Table(
                name=t["name"], columns=cols, pk=list(t["pk_cols"]),
                fks=edges.get(t["name"], []),
            )
        return cls(tables=tables)


@dataclass
class TableConfig:
    mode: str                       # natural | value | hash | reload | out_of_scope
    identity: list[str] = field(default_factory=list)
    hash_exclude: list[str] = field(default_factory=list)
    sequence: str | None = None     # target sequence for surrogate allocation
    manual_fks: list[dict] = field(default_factory=list)  # unenforced logical FKs
    delete_orphans: bool = False    # owned child: mirror Source's child set within matched parents


@dataclass
class Config:
    tables: dict[str, TableConfig]
    audit_exclude: list[str]
    staging_schema: str
    target_schema: str
    constraint_handling: str = "disable"   # disable | defer | none
    # audit column -> SQL expression written on insert/update instead of the source value
    audit_override: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        import yaml  # lazy; only needed when a config is loaded
        with open(path) as fh:
            doc = yaml.safe_load(fh)
        tables = {
            name: TableConfig(
                mode=spec["mode"],
                identity=list(spec.get("identity", [])),
                hash_exclude=list(spec.get("hash_exclude", [])),
                sequence=spec.get("sequence"),
                manual_fks=list(spec.get("manual_fks", [])),
                delete_orphans=bool(spec.get("delete_orphans", False)),
            )
            for name, spec in doc["tables"].items()
        }
        return cls(
            tables=tables,
            audit_exclude=list(doc.get("audit_exclude", [])),
            staging_schema=doc["staging_schema"],
            target_schema=doc["target_schema"],
            constraint_handling=doc.get("constraint_handling", "disable"),
            audit_override=dict(doc.get("audit_override", {})),
        )
