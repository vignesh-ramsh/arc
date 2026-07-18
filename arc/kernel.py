"""
arc.kernel
-----------------
The runtime capability registry — the object every plugin's `register(kernel)`
function talks to, and what `arc.boot()` returns.

This is NOT arc/registry.py. That module is the CLI's bookkeeping for
`.arc/plugins.lock` (what is installed and enabled *on disk*). This module is
the in-process registry that exists only for the lifetime of a booted process.

Domain-blind by design (Architecture §3.1): the Kernel only ever sees a string
name, an instance, a `requires` list, and an `optional_requires` list. It
never defines what a capability *is* — no typed Protocols, no domain
knowledge. Adding a new capability type must cost zero kernel code changes,
permanently.
"""

from __future__ import annotations

import sys
import typing
import warnings as _warnings
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

if typing.TYPE_CHECKING:
    from .resolver import BootPlan, PluginSpec
    from .settings import SettingsManager


class ArcAdvisory(UserWarning):
    """
    A non-fatal boot/runtime advisory (Architecture §3.5): something worth
    knowing that must never block startup — e.g. "local-file secrets in use".
    Emitted via warnings.warn() and also collected on Kernel.advisories.
    Placeholder channel until arc.health exists as a real runtime API.
    """


class KernelError(RuntimeError):
    """Base class for all runtime kernel failures."""


class ExportError(KernelError):
    """A plugin's export was invalid — always a hard boot failure."""


# Capability names that can never be exported. Anything here would be found
# by normal attribute lookup on the `arc` module *before* PEP 562
# __getattr__ ever fires, silently shadowing the capability. The static set
# covers the kernel's own submodules and public API; is_reserved_capability_name()
# additionally checks the live `arc` module namespace so nothing slips through.
RESERVED_CAPABILITY_NAMES = frozenset({
    # kernel submodules
    "cli", "kernel", "registry", "resolver", "runtime", "secrets", "settings",
    "doctor", "plugin_cli", "codec", "health", "events",
    # public API re-exported on the arc package
    "boot", "shutdown", "find_project_root",
    "Kernel", "Capability", "KernelError", "ExportError",
    "BootError", "ResolutionError", "ArcAdvisory",
    "BootPlan", "PluginSpec",
    "__version__",
})


def is_reserved_capability_name(name: str) -> bool:
    if name in RESERVED_CAPABILITY_NAMES:
        return True
    arc_module = sys.modules.get("arc")
    # vars() reads the module dict directly — it does NOT trigger the
    # package's __getattr__, so booted capabilities never look "reserved".
    return arc_module is not None and name in vars(arc_module)


def capability_name_problem(name: object) -> str | None:
    """Return a human-readable problem with a proposed capability name, or None if valid."""
    if not isinstance(name, str) or not name:
        return "capability name must be a non-empty string"
    if not name.isidentifier():
        return (
            f"'{name}' is not a valid Python identifier — a capability becomes "
            f"the attribute `arc.{name}`, so it must be importable as one"
        )
    if name.startswith("_"):
        return f"'{name}' starts with an underscore, which is reserved for arc internals"
    if is_reserved_capability_name(name):
        return (
            f"'{name}' collides with the arc module's own namespace (a kernel "
            f"submodule or public API attribute) and would be shadowed — "
            f"pick a different capability name"
        )
    return None


@dataclass(frozen=True)
class Capability:
    """One exported capability: exactly what §3.1 says the kernel may know."""
    name: str
    instance: Any
    requires: tuple[str, ...]
    optional_requires: tuple[str, ...]
    plugin: str  # which plugin exported it ("<direct export>" outside boot)


class Kernel:
    """
    The runtime capability registry.

    Created by arc.boot(); plugins receive it as the sole argument to their
    register(kernel) function and call kernel.export(...) exactly once.
    Can also be constructed directly (tests, embedding) — spec cross-checks
    are then skipped, but name validation and duplicate/require enforcement
    still apply.
    """

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        settings: "SettingsManager | None" = None,
        plan: "BootPlan | None" = None,
    ) -> None:
        self.project_root = project_root
        #: The project-bound SettingsManager (or None when constructed
        #: directly outside a project). `arc.settings.get/set/...` at module
        #: level proxy to this — one call site, secret or not (§3.5).
        self.settings = settings
        self.advisories: list[str] = []
        self._plan = plan
        self._caps: dict[str, Capability] = {}
        self._current_spec: "PluginSpec | None" = None
        self._current_export_count = 0

    # ------------------------------------------------------------------ #
    # The registration surface plugins see (§3.1)
    # ------------------------------------------------------------------ #
    def export(
        self,
        name: str,
        instance: Any,
        requires: Iterable[str] = (),
        optional_requires: Iterable[str] = (),
    ) -> None:
        """
        Export `instance` as the capability `name` — reachable as arc.<name>
        after boot. Hard `requires` must already be registered (boot's
        topological order guarantees this when manifests are accurate).
        """
        requires = tuple(requires or ())
        optional_requires = tuple(optional_requires or ())

        problem = capability_name_problem(name)
        if problem:
            who = f"plugin '{self._current_spec.name}'" if self._current_spec else "a direct export"
            raise ExportError(f"invalid capability export from {who}: {problem}.")

        if name in self._caps:
            raise ExportError(
                f"capability '{name}' is already exported by plugin "
                f"'{self._caps[name].plugin}' — two plugins exporting the same "
                f"name is a hard boot failure (§3.1); rename one."
            )

        spec = self._current_spec
        if spec is not None:
            if self._current_export_count >= 1:
                raise ExportError(
                    f"plugin '{spec.name}' attempted to export a second "
                    f"capability ('{name}') — each plugin exports exactly one "
                    f"namespace under arc.<capability> (§2)."
                )
            if name != spec.capability:
                raise ExportError(
                    f"plugin '{spec.name}' exported capability '{name}', but its "
                    f"manifest (plugin.toml, via plugins.lock) declares "
                    f"capability = \"{spec.capability}\". Align register() and "
                    f"plugin.toml, then run `arc build` to refresh the lock."
                )
            if set(requires) != set(spec.requires) or set(optional_requires) != set(
                spec.optional_requires
            ):
                self.advise(
                    f"manifest drift in plugin '{spec.name}': register() declared "
                    f"requires={sorted(requires)} / optional_requires="
                    f"{sorted(optional_requires)}, but plugins.lock has "
                    f"requires={sorted(spec.requires)} / optional_requires="
                    f"{sorted(spec.optional_requires)}. Load order was computed "
                    f"from the lock — update plugin.toml and run `arc build`."
                )

        for req in requires:
            if req not in self._caps:
                raise ExportError(self._missing_requirement_message(name, spec, req))

        self._caps[name] = Capability(
            name=name,
            instance=instance,
            requires=requires,
            optional_requires=optional_requires,
            plugin=spec.name if spec else "<direct export>",
        )
        self._current_export_count += 1

    # ------------------------------------------------------------------ #
    # Lookup — what arc/__init__'s PEP 562 __getattr__ resolves against
    # ------------------------------------------------------------------ #
    def get(self, name: str) -> Any:
        """Return the instance exported as `name`."""
        try:
            return self._caps[name].instance
        except KeyError:
            raise KernelError(
                f"no capability named '{name}' is registered "
                f"(registered: {sorted(self._caps) or 'none'})."
            ) from None

    def has(self, name: str) -> bool:
        return name in self._caps

    def capabilities(self) -> Mapping[str, Capability]:
        """Read-only view of every exported capability, keyed by name."""
        return MappingProxyType(self._caps)

    def current_plugin(self) -> str | None:
        """The name of the plugin whose register(kernel) is currently
        executing, or None outside of one. Lets a provider (e.g. psqldb)
        attribute something a caller declares — a set of schema files, say
        — to whichever plugin declared it, without that caller having to
        pass its own name by hand. Still just a string (§3.1): the kernel
        doesn't know or care what the provider does with it."""
        return self._current_spec.name if self._current_spec else None

    def advise(self, message: str) -> None:
        """Record a non-fatal advisory (§3.5) — collected AND warned, never raised."""
        self.advisories.append(message)
        _warnings.warn(message, ArcAdvisory, stacklevel=3)

    # ------------------------------------------------------------------ #
    # Internal — called by arc.runtime around each register() invocation
    # ------------------------------------------------------------------ #
    def _begin_plugin(self, spec: "PluginSpec") -> None:
        self._current_spec = spec
        self._current_export_count = 0

    def _finish_plugin(self, spec: "PluginSpec") -> None:
        try:
            if self._current_export_count == 0:
                raise ExportError(
                    f"plugin '{spec.name}' register() completed without exporting "
                    f"its capability '{spec.capability}' — register(kernel) must "
                    f"call kernel.export(...) exactly once."
                )
        finally:
            self._current_spec = None
            self._current_export_count = 0

    def _missing_requirement_message(
        self, name: str, spec: "PluginSpec | None", req: str
    ) -> str:
        base = (
            f"capability '{name}' declares a hard require on '{req}', "
            f"which is not registered"
        )
        plan = self._plan
        if plan is not None:
            later = next((s for s in plan.load_order if s.capability == req), None)
            if later is not None:
                return (
                    base
                    + f" yet — plugin '{later.name}' provides it but loads later. "
                    f"The requirement is missing from the manifest that load "
                    f"order was computed from: add '{req}' to `requires` in "
                    f"plugin.toml and run `arc build`."
                )
            skipped = next(
                (sk for sk in plan.skipped if sk.capability == req), None
            )
            if skipped is not None:
                return (
                    base
                    + f" — plugin '{skipped.name}' provides it but was skipped "
                    f"({skipped.reason})."
                )
        return (
            base
            + ". No plugin in this boot provides it — install one "
            "(`arc install <git-url>`) or drop the requirement."
        )