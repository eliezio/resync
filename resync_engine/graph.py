"""Topological sort, parents first (see DESIGN.md, decision 5).

The entity graph must be a **DAG with no self-references**. Identity resolves parent-first, so the
merge needs an ordering, and a cycle denies one. Every cycle raises CycleError — including a
self-reference, which is a length-1 cycle.

An earlier version broke a cycle whose back-edge was a nullable FK, inserting that column NULL and
filling it in on a second pass. That was removed: no schema in scope has a cycle, the fixup only
ever worked for tables carrying their own surrogate id-map, and a half-supported topology is worse
than a loud refusal. If a cycle ever appears, this is the alarm.
"""
from __future__ import annotations

from .model import Schema


class CycleError(Exception):
    pass


def load_order(schema: Schema, scope: set[str],
               extra_parents: dict[str, set[str]] | None = None) -> list[str]:
    """Kahn topological sort over `scope`, parents first.

    `extra_parents` adds edges from manually declared FKs that the database does not enforce.
    """
    parents: dict[str, set[str]] = {t: set() for t in scope}
    children: dict[str, set[str]] = {t: set() for t in scope}

    def add(child: str, parent: str) -> None:
        if parent == child:
            raise CycleError(
                f"self-reference on {child}: the entity graph must be acyclic (DESIGN.md, decision 5)")
        if parent in scope:
            parents[child].add(parent)
            children[parent].add(child)

    for t in scope:
        for fk in schema.tables[t].fks:
            add(t, fk.parent)
    for child, ps in (extra_parents or {}).items():
        if child in scope:
            for parent in ps:
                add(child, parent)

    remaining = {t: set(p) for t, p in parents.items()}
    order: list[str] = []
    ready = sorted(t for t in scope if not remaining[t])

    while ready:
        n = ready.pop(0)
        order.append(n)
        for c in sorted(children[n]):
            if n in remaining[c]:
                remaining[c].discard(n)
                if not remaining[c]:
                    ready.append(c)
                    ready.sort()

    if len(order) != len(scope):
        stuck = sorted(set(scope) - set(order))
        raise CycleError(f"cycle detected among: {', '.join(stuck)}")
    return order
