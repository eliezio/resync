"""Generate a draft resync.yaml from config.skeleton.json.

Seeds the confident classifications from the schema and marks every judgment call with a
TODO/CONFIRM comment plus candidates. It deliberately does NOT infer domain facts that need human
knowledge — a mutable column used in a unique key, ownership/delete policy, unenforced logical
foreign keys, or sequence names — those are flagged, not guessed (see DESIGN.md decision 3).

Heuristics (schema-only, so approximate):
  * surrogate  = a single-column PK ending in `_ID` that is not itself a natural unique.
  * reference  = a code primary key (single PK, not a surrogate) -> natural, identity = PK.
  * natural    = surrogate PK + a non-surrogate unique -> identity = that unique.
  * value      = composite PK with no surrogate -> identity = PK columns; owned-child if the PK is
                 entirely FK + code/discriminator columns.
  * unknown    = surrogate PK with no unique -> TODO, with candidate columns listed.
  * out_of_scope = views (`*_V`) and isolated tables with no inferable identity.
"""
from __future__ import annotations

import json
import os
import re

HIDDEN = "SYS_NC"
_AUDIT = re.compile(r"^(UPDATE|INSERT)_(ID|TMSTMP)$|^VERSION_ID$|_IND$")


def _is_audit(col: str) -> bool:
    return bool(_AUDIT.search(col))


def _visible(cols: list[str]) -> list[str]:
    return [c for c in cols if not c.startswith(HIDDEN)]


def _classify(name, cols, pk, uniq, fkcols, surrogate, isolated) -> str:
    out = [f"  {name}:"]

    def seq_hint():
        if surrogate:
            out.append(f"    # sequence: {name}_SEQ   # TODO confirm the real sequence name")

    if name.endswith("_V"):
        out.append("    mode: out_of_scope   # view, not a base table")
        return "\n".join(out)

    if isolated and not uniq and surrogate:
        cand = [c for c in cols if c.endswith(("_CD", "_NAME", "_NO"))]
        out.append("    mode: out_of_scope   # TODO isolated, no enforced natural key")
        out.append(f"    # if it must sync, give it an identity. candidates: {cand}")
        return "\n".join(out)

    if len(pk) >= 2 and not surrogate:                       # composite PK -> value object
        out.append("    mode: value")
        out.append(f"    identity: {pk}")
        if all((c in fkcols) or c.endswith(("_CD", "_NO", "_SEQ", "_KEY")) for c in pk):
            out.append("    delete_orphans: true   # CONFIRM owned child (mirror within parent)")
        out.append("    # CONFIRM identity; ensure no column is a mutable/versioned value")
        return "\n".join(out)

    if len(pk) == 1 and not surrogate:                       # code primary key
        out.append("    mode: natural")
        out.append(f"    identity: {pk}   # code primary key")
        return "\n".join(out)

    if surrogate and uniq:                                   # surrogate + natural unique
        out.append("    mode: natural")
        out.append(f"    identity: {uniq}")
        seq_hint()
        if len(uniq) > 1 or any(u.endswith("_ID") for u in uniq):
            out.append("    # CONFIRM natural key (multiple / derived unique columns)")
        out.append("    # CONFIRM no identity column is a mutable counter (VERSION / updated marker)")
        return "\n".join(out)

    if surrogate and not uniq:                               # surrogate, no unique -> unknown
        cand = [c for c in cols if c.endswith(("_CD", "_NAME", "_NO"))]
        out.append("    mode: value   # TODO no enforced natural key")
        out.append(f"    identity: []   # TODO parent FK + discriminator. candidates: {cand}")
        seq_hint()
        return "\n".join(out)

    out.append("    mode: value   # TODO classify")
    out.append(f"    identity: {pk or []}")
    return "\n".join(out)


def seed_config(catalog_path: str, existing_path: str | None = None,
                staging: str = "RESYNC_STG", target: str = "TARGET") -> str:
    doc = json.load(open(catalog_path))
    tables = {t["name"]: t for t in doc["tables"]}

    child_fk_cols: dict[str, set[str]] = {}
    has_parent: set[str] = set()
    referenced: set[str] = set()
    for e in doc["edges"]:
        child_fk_cols.setdefault(e["child"], set()).update(p.split("->")[0] for p in e["columns"])
        has_parent.add(e["child"])
        referenced.add(e["parent"])

    existing: set[str] = set()
    if existing_path and os.path.exists(existing_path):
        import yaml
        ex = yaml.safe_load(open(existing_path)) or {}
        existing = set((ex.get("tables") or {}).keys())

    audit_cols: set[str] = set()
    blocks: list[str] = []
    for name in sorted(tables):
        if name in existing:
            continue
        t = tables[name]
        cols = _visible([c["name"] for c in t["columns"]])
        pk = _visible(t.get("pk_cols", []))
        uniq = _visible(t.get("uniq_candidate_cols", []))
        audit_cols.update(c for c in cols if _is_audit(c))
        surrogate = pk[0] if (len(pk) == 1 and pk[0] not in uniq and pk[0].endswith("_ID")) else None
        isolated = name not in referenced and name not in has_parent
        blocks.append(_classify(name, cols, pk, uniq, child_fk_cols.get(name, set()),
                                surrogate, isolated))

    audit = sorted(audit_cols) or ["UPDATE_ID", "UPDATE_TMSTMP", "VERSION_ID"]
    header = (
        "# Draft resync.yaml generated by `seed-config`. REVIEW every TODO / CONFIRM before use.\n"
        "# Confident classifications are seeded from the schema; domain judgment (mutable-in-identity\n"
        "# columns, ownership/delete policy, logical manual_fks, sequence names) is flagged, not guessed.\n\n"
        f"audit_exclude: {audit}\n"
        f"staging_schema: {staging}\n"
        f"target_schema: {target}\n\n"
        "tables:\n\n"
    )
    return header + "\n".join(blocks) + "\n"
