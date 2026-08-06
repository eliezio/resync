"""Topological sort with nullable-cycle breaking (see DESIGN.md, decision 5).

Parents are processed before children. A cycle normally raises CycleError, but a cycle that can
be broken by a **nullable** foreign key is broken instead: that edge is recorded as *deferred*.
The engine then inserts the deferred FK column(s) as NULL and sets them in a second pass once all
rows exist. Self-references are a length-1 cycle and are always deferred when nullable. A cycle
whose edges are all non-nullable (including a non-nullable self-reference) still fails loud.
"""
from __future__ import annotations

from .model import ForeignKey, Schema


class CycleError(Exception):
    pass


def load_order(schema: Schema, scope: set[str],
               extra_parents: dict[str, set[str]] | None = None,
               deferred: list[tuple[str, ForeignKey]] | None = None) -> list[str]:
    """Kahn topological sort over `scope`, parents first.

    `extra_parents` adds required (non-breakable) edges from manually declared FKs.
    `deferred`, if given, is populated with (table, foreign_key) for each edge dropped to break a
    nullable cycle — the caller nulls those columns on insert and fixes them in a second pass.
    """
    dropped = deferred if deferred is not None else []
    parents: dict[str, set[str]] = {t: set() for t in scope}
    children: dict[str, set[str]] = {t: set() for t in scope}
    breakable: dict[tuple[str, str], ForeignKey] = {}

    def add(child: str, parent: str, fk: ForeignKey | None = None) -> None:
        if parent == child:                      # self-reference: never orderable
            if fk is not None:
                if fk.nullable:
                    dropped.append((child, fk))
                else:
                    raise CycleError(f"non-nullable self-reference on {child}")
            return
        if parent in scope:
            parents[child].add(parent)
            children[parent].add(child)
            if fk is not None and fk.nullable:
                breakable[(child, parent)] = fk

    for t in scope:
        for fk in schema.tables[t].fks:
            add(t, fk.parent, fk)
    for child, ps in (extra_parents or {}).items():
        if child in scope:
            for parent in ps:
                add(child, parent)               # manual FKs are required, not breakable

    remaining = {t: set(p) for t, p in parents.items()}
    order: list[str] = []
    ready = sorted(t for t in scope if not remaining[t])

    while len(order) < len(scope):
        while ready:
            n = ready.pop(0)
            order.append(n)
            for c in sorted(children[n]):
                if n in remaining[c]:
                    remaining[c].discard(n)
                    if not remaining[c]:
                        ready.append(c)
                        ready.sort()
        if len(order) == len(scope):
            break
        # Stuck on a cycle — break one nullable back-edge among the unresolved nodes.
        done = set(order)
        stuck = [t for t in scope if t not in done]
        broke = False
        for c in sorted(stuck):
            for parent in sorted(remaining[c]):
                if (c, parent) in breakable:
                    dropped.append((c, breakable.pop((c, parent))))
                    remaining[c].discard(parent)
                    if not remaining[c]:
                        ready.append(c)
                        ready.sort()
                    broke = True
                    break
            if broke:
                break
        if not broke:
            raise CycleError(f"cycle detected among: {', '.join(sorted(stuck))}")
    return order
