"""
arc.kernel.resolver
===================
Turns an unordered set of plugins into a deterministic startup order.

Hybrid model (the chosen design):

  1. Capability edges decide *correctness*. If plugin B ``requires`` a
     capability that plugin A ``provides``, then A must come before B.
  2. ``load_order`` (then name) is only a *tiebreak* among plugins that are
     otherwise free to go in any order — it never overrides a capability edge.

This is why nobody writes ``load_order=0`` for the database anymore: every
plugin that touches the DB ``requires "db.session"``, so db is forced first by
the graph itself. Arc orchestrates; the author just declares needs.

Failures are loud and early: a missing provider or a dependency cycle raises
``ResolutionError`` before any plugin is configured.
"""

from __future__ import annotations

from dataclasses import dataclass

from arc.kernel.exceptions import CapabilityError, ResolutionError
from arc.kernel.plugin import Plugin


@dataclass(frozen=True)
class ResolvedGraph:
    order: list[Plugin]
    provider_of: dict[str, str]  # capability -> plugin name

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.order]


def resolve(plugins: list[Plugin]) -> ResolvedGraph:
    """Return plugins in dependency-respecting, deterministic order."""
    by_name = {p.name: p for p in plugins}
    if len(by_name) != len(plugins):
        seen: set[str] = set()
        dupes = {p.name for p in plugins if p.name in seen or seen.add(p.name)}
        raise ResolutionError(
            f"Duplicate plugin name(s): {', '.join(sorted(dupes))}",
            code="arc.resolve.duplicate",
        )

    # Map every provided capability to its single provider.
    provider_of: dict[str, str] = {}
    for p in plugins:
        for cap in p.provides:
            if cap in provider_of:
                raise CapabilityError(
                    f"Capability '{cap}' provided by both "
                    f"'{provider_of[cap]}' and '{p.name}'.",
                    code="arc.capability.duplicate",
                )
            provider_of[cap] = p.name

    # Build dependency edges: requirer depends on provider.
    deps: dict[str, set[str]] = {p.name: set() for p in plugins}
    for p in plugins:
        for cap in p.requires:
            provider = provider_of.get(cap)
            if provider is None:
                raise ResolutionError(
                    f"Plugin '{p.name}' requires capability '{cap}', "
                    f"which no plugin provides.",
                    code="arc.resolve.missing_provider",
                    detail={"plugin": p.name, "capability": cap},
                )
            if provider != p.name:
                deps[p.name].add(provider)

    # Kahn topological sort with a (load_order, name) tiebreak among ready nodes.
    indegree = {name: len(deps[name]) for name in deps}
    dependents: dict[str, set[str]] = {name: set() for name in deps}
    for name, requires in deps.items():
        for dep in requires:
            dependents[dep].add(name)

    def sort_key(name: str) -> tuple[int, str]:
        return (by_name[name].load_order, name)

    ready = sorted(
        (n for n, d in indegree.items() if d == 0), key=sort_key
    )
    order: list[str] = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for dep in dependents[name]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                # Insert keeping the ready list sorted by tiebreak.
                ready.append(dep)
                ready.sort(key=sort_key)

    if len(order) != len(plugins):
        stuck = sorted(set(by_name) - set(order))
        raise ResolutionError(
            f"Dependency cycle among plugins: {', '.join(stuck)}",
            code="arc.resolve.cycle",
            detail={"unresolved": stuck},
        )

    return ResolvedGraph(order=[by_name[n] for n in order], provider_of=provider_of)
