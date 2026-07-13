"""
arc.resolver
-------------------
Pure, side-effect-free boot planning: turn `.arc/plugins.lock` plus the
installed `arc.plugins` entry points into an ordered, validated BootPlan.

Shared by two consumers with deliberately different natures:

  * arc.runtime.boot()  — resolves a plan, then EXECUTES it,
  * `arc doctor` (CLI)  — resolves a plan, then only PRINTS it.

To keep the doctor path a true dry run, this module never imports plugin
code: entry points are matched by name only, never `.load()`ed here.

Resolution rules (Architecture §3.1 / §3.3 / §3.6):
  * plugins.lock is the source of truth for what is enabled — an entry point
    installed in the venv but absent from the lock is skipped with a warning
    (run `arc build`), never silently loaded;
  * an ENABLED lock entry with no matching entry point is a hard failure;
  * duplicate capability names among enabled plugins → hard failure;
  * missing hard `requires` → hard failure, with a targeted hint when the
    provider exists but is disabled;
  * `optional_requires` that are absent are simply fine; when present they
    influence load order (dependency first) unless that would create a
    cycle, in which case the optional edge is dropped with a warning;
  * load order is deterministic: topological, alphabetical tie-break.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field, replace
from importlib.metadata import EntryPoint
from importlib.metadata import entry_points as _installed_entry_points
from pathlib import Path
from typing import Any, Iterable

import tomlkit

from .kernel import KernelError, capability_name_problem
from .registry import load_lock

ENTRY_POINT_GROUP = "arc.plugins"


class ResolutionError(KernelError):
    """Boot planning failed — arc.boot() would not be able to start."""


@dataclass(frozen=True)
class PluginSpec:
    """One plugin as declared by plugins.lock, optionally matched to an entry point."""
    name: str
    capability: str
    version: str
    requires: tuple[str, ...]
    optional_requires: tuple[str, ...]
    enabled: bool
    # importlib.metadata.EntryPoint in production; tests inject duck-typed
    # fakes exposing .name / .value / .load().
    entry_point: EntryPoint | Any | None = None


@dataclass(frozen=True)
class SkippedPlugin:
    name: str
    reason: str
    capability: str | None = None


@dataclass
class BootPlan:
    """Everything arc.boot() would do, computed without doing any of it."""
    project_root: Path | None
    load_order: list[PluginSpec]
    skipped: list[SkippedPlugin] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "project_root": str(self.project_root) if self.project_root else None,
            "load_order": [
                {
                    "position": i,
                    "name": s.name,
                    "capability": s.capability,
                    "version": s.version,
                    "requires": list(s.requires),
                    "optional_requires": list(s.optional_requires),
                    "entry_point": getattr(s.entry_point, "value", None),
                }
                for i, s in enumerate(self.load_order, start=1)
            ],
            "skipped": [
                {"plugin": sk.name, "capability": sk.capability, "reason": sk.reason}
                for sk in self.skipped
            ],
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def discover_entry_points() -> tuple[EntryPoint, ...]:
    """All installed entry points in the `arc.plugins` group. Nothing is imported."""
    return tuple(_installed_entry_points(group=ENTRY_POINT_GROUP))


def specs_from_lock(lock_doc: tomlkit.TOMLDocument) -> list[PluginSpec]:
    plugins_table = lock_doc.get("plugins", {}) or {}
    specs: list[PluginSpec] = []
    for name, entry in plugins_table.items():
        specs.append(
            PluginSpec(
                name=str(name),
                capability=str(entry.get("capability", name)),
                version=str(entry.get("version", "0.0.0")),
                requires=tuple(str(r) for r in (entry.get("requires", []) or [])),
                optional_requires=tuple(
                    str(r) for r in (entry.get("optional_requires", []) or [])
                ),
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return sorted(specs, key=lambda s: s.name)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def resolve(
    project_root: Path | None = None,
    *,
    lock_doc: tomlkit.TOMLDocument | None = None,
    entry_points: Iterable[Any] | None = None,
) -> BootPlan:
    """
    Compute a BootPlan. Raises ResolutionError for anything that would be a
    hard boot failure; everything advisory lands in BootPlan.warnings.
    """
    if lock_doc is None:
        if project_root is None:
            lock_doc = tomlkit.document()
        else:
            lock_doc = load_lock(Path(project_root) / ".arc" / "plugins.lock")
    eps = tuple(entry_points) if entry_points is not None else discover_entry_points()

    warnings_: list[str] = []
    skipped: list[SkippedPlugin] = []
    specs = specs_from_lock(lock_doc)

    # -- entry points: unique names, indexed --------------------------------
    eps_by_name: dict[str, Any] = {}
    for ep in eps:
        if ep.name in eps_by_name:
            raise ResolutionError(
                f"two installed packages both provide an '{ENTRY_POINT_GROUP}' "
                f"entry point named '{ep.name}' "
                f"('{getattr(eps_by_name[ep.name], 'value', '?')}' and "
                f"'{getattr(ep, 'value', '?')}') — plugin names must be unique; "
                f"uninstall or rename one."
            )
        eps_by_name[ep.name] = ep

    # -- entry points installed but unknown to the lock: never loaded -------
    known_names = {s.name for s in specs}
    for stray in sorted(set(eps_by_name) - known_names):
        reason = "installed in the environment but not present in plugins.lock"
        skipped.append(SkippedPlugin(name=stray, reason=reason))
        warnings_.append(
            f"entry point '{stray}' ({ENTRY_POINT_GROUP}) is {reason} — it will "
            f"NOT be loaded. If it is a real plugin of this project, run "
            f"`arc build` to register it; otherwise remove it from the venv."
        )

    # -- lock entries: split enabled/disabled, demand entry points ----------
    enabled: list[PluginSpec] = []
    for spec in specs:
        if not spec.enabled:
            skipped.append(
                SkippedPlugin(
                    name=spec.name,
                    capability=spec.capability,
                    reason="disabled in plugins.lock "
                    f"(`arc plugin enable {spec.name}` to load it)",
                )
            )
            continue
        ep = eps_by_name.get(spec.name)
        if ep is None:
            raise ResolutionError(
                f"plugin '{spec.name}' is enabled in plugins.lock, but no "
                f"'{ENTRY_POINT_GROUP}' entry point named '{spec.name}' is "
                f"installed in this environment. The plugin's pyproject.toml "
                f"must declare\n"
                f'    [project.entry-points."{ENTRY_POINT_GROUP}"]\n'
                f'    {spec.name} = "<package>:register"\n'
                f"then run `arc build` (or `uv sync --all-packages`) to install it."
            )
        enabled.append(replace(spec, entry_point=ep))

    # -- capability names: valid, non-reserved, unique among enabled --------
    by_capability: dict[str, PluginSpec] = {}
    for spec in enabled:
        problem = capability_name_problem(spec.capability)
        if problem:
            raise ResolutionError(
                f"plugin '{spec.name}' declares an invalid capability: {problem}."
            )
        clash = by_capability.get(spec.capability)
        if clash is not None:
            raise ResolutionError(
                f"plugins '{clash.name}' and '{spec.name}' both export capability "
                f"'{spec.capability}' — capability names must be unique (§3.1); "
                f"rename one, or `arc plugin disable` one of them."
            )
        by_capability[spec.capability] = spec

    # -- hard requires must be satisfiable by the ENABLED set ---------------
    disabled_by_capability = {s.capability: s for s in specs if not s.enabled}
    for spec in enabled:
        for req in spec.requires:
            if req in by_capability:
                continue
            provider = disabled_by_capability.get(req)
            if provider is not None:
                raise ResolutionError(
                    f"plugin '{spec.name}' requires capability '{req}', which is "
                    f"provided by plugin '{provider.name}' — currently DISABLED. "
                    f"Run `arc plugin enable {provider.name}`, or disable "
                    f"'{spec.name}' as well."
                )
            raise ResolutionError(
                f"plugin '{spec.name}' requires capability '{req}', but no "
                f"enabled plugin provides it. Install one "
                f"(`arc install <git-url>`) or disable '{spec.name}'."
            )

    load_order = _topological_order(enabled, warnings_)
    return BootPlan(
        project_root=project_root,
        load_order=load_order,
        skipped=skipped,
        warnings=warnings_,
    )


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #
def _kahn_unresolved(adjacency: dict[str, set[str]]) -> set[str]:
    """Nodes left over after a Kahn pass — non-empty iff the graph has a cycle."""
    indegree = {node: 0 for node in adjacency}
    for outs in adjacency.values():
        for out in outs:
            indegree[out] += 1
    queue = deque(node for node, deg in indegree.items() if deg == 0)
    while queue:
        node = queue.popleft()
        for out in adjacency[node]:
            indegree[out] -= 1
            if indegree[out] == 0:
                queue.append(out)
    return {node for node, deg in indegree.items() if deg > 0}


def _topological_order(
    specs: list[PluginSpec], warnings_: list[str]
) -> list[PluginSpec]:
    """
    Order specs so every hard dependency registers before its dependents.
    Edges run dependency -> dependent. Hard edges are mandatory (a cycle is a
    hard failure); optional edges are honored when possible and dropped with a
    warning when they would close a cycle. Ties break alphabetically by
    capability so load order is fully deterministic.
    """
    by_capability = {s.capability: s for s in specs}
    adjacency: dict[str, set[str]] = {cap: set() for cap in by_capability}

    for spec in specs:
        for req in spec.requires:  # presence already validated by resolve()
            adjacency[req].add(spec.capability)

    unresolved = _kahn_unresolved(adjacency)
    if unresolved:
        detail = "; ".join(
            f"'{by_capability[cap].name}' requires {list(by_capability[cap].requires)}"
            for cap in sorted(unresolved)
        )
        raise ResolutionError(
            f"hard dependency cycle among plugins: {detail} — cycles in "
            f"`requires` cannot be booted; make one side an optional_requires "
            f"or break the dependency."
        )

    def reaches(src: str, dst: str) -> bool:
        stack, seen = [src], {src}
        while stack:
            node = stack.pop()
            if node == dst:
                return True
            for out in adjacency[node]:
                if out not in seen:
                    seen.add(out)
                    stack.append(out)
        return False

    for spec in sorted(specs, key=lambda s: s.capability):
        for opt in sorted(set(spec.optional_requires)):
            if opt not in by_capability:
                continue  # absent optional — allowed, plugin handles it (§3.3)
            if spec.capability in adjacency[opt]:
                continue  # already an edge (also a hard require)
            if reaches(spec.capability, opt):
                warnings_.append(
                    f"optional ordering skipped: '{spec.name}' optionally "
                    f"requires '{opt}', but loading '{opt}' first would create a "
                    f"dependency cycle. '{by_capability[opt].name}' will register "
                    f"AFTER '{spec.name}', so '{spec.name}' must look it up "
                    f"lazily (post-boot) rather than at register() time."
                )
                continue
            adjacency[opt].add(spec.capability)

    indegree = {cap: 0 for cap in by_capability}
    for outs in adjacency.values():
        for out in outs:
            indegree[out] += 1
    heap = [cap for cap, deg in indegree.items() if deg == 0]
    heapq.heapify(heap)
    order: list[PluginSpec] = []
    while heap:
        cap = heapq.heappop(heap)
        order.append(by_capability[cap])
        for out in sorted(adjacency[cap]):
            indegree[out] -= 1
            if indegree[out] == 0:
                heapq.heappush(heap, out)

    if len(order) != len(specs):  # unreachable: optional edges never close cycles
        raise ResolutionError(
            "internal resolver error: ordering did not cover every plugin."
        )
    return order