"""Topological sort and cycle detection over the FK graph.

Edge direction: child depends on parent (child has an FK to parent), so parents must be
processed first. The returned order is parents-first. Self-references and multi-table cycles
raise CycleError — the engine refuses to run against a cyclic graph (see DESIGN.md, decision 5).
"""
from __future__ import annotations

from .model import Schema


class CycleError(Exception):
    pass


def load_order(schema: Schema, scope: set[str],
               extra_parents: dict[str, set[str]] | None = None) -> list[str]:
    """Kahn topological sort restricted to `scope`; parents first.

    Only edges whose parent is also in scope constrain the order — an FK to an out-of-scope
    table (e.g. a reference table left untouched) imposes no ordering here.
    """
    parents: dict[str, set[str]] = {t: set() for t in scope}
    children: dict[str, set[str]] = {t: set() for t in scope}
    def _add(child: str, parent: str) -> None:
        if parent in scope and parent != child:
            parents[child].add(parent)
            children[parent].add(child)

    for t in scope:
        for fk in schema.tables[t].fks:
            _add(t, fk.parent)
    # Manually declared (unenforced) FKs — invisible to the catalog but they still constrain order.
    for child, ps in (extra_parents or {}).items():
        if child in scope:
            for parent in ps:
                _add(child, parent)

    ready = sorted(t for t in scope if not parents[t])
    order: list[str] = []
    remaining = {t: set(p) for t, p in parents.items()}
    while ready:
        n = ready.pop(0)
        order.append(n)
        for c in sorted(children[n]):
            remaining[c].discard(n)
            if not remaining[c]:
                ready.append(c)
                ready.sort()

    if len(order) != len(scope):
        stuck = sorted(scope - set(order))
        raise CycleError(f"cycle detected among: {', '.join(stuck)}")
    return order
