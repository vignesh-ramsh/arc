"""
arc.kernel.plugin
================
The ``Plugin`` base class. *Everything* in Arc is a plugin — db, api, http,
and your own ``hr``/``finance`` plugins all subclass this and are listed in
arc.lock as equals. The kernel has no privileged knowledge of any of them.

Declaration
-----------
    provides          capabilities this plugin offers     ("db.session",)
    requires          capabilities this plugin needs       ("db.session",)
    requires_optional capabilities used *if present*; absence is NOT fatal and
                      the plugin must degrade gracefully    ("cache.client",)
    load_order        tiebreak only — capability edges decide real order
    critical          a failed startup_check aborts the app

``requires`` is hard: a missing provider aborts the build at resolve time.
``requires_optional`` is soft: if some plugin provides it, an ordering edge is
created (provider loads first); if nobody provides it, it is silently skipped.
Consume optional capabilities with ``rt.capabilities.get(name)`` (returns None
when absent), never ``require(name)`` (which raises).

Two synchronous wiring passes (before any event loop):
    setup(rt)       register provided capabilities into rt.capabilities
    contribute(rt)  add routes / cli / schemas into rt.extensions
                    (runs after *every* plugin's setup, so requires are ready)

Three async lifecycle hooks (inside the ASGI lifespan / headless run):
    startup()   open connections, warm caches
    ready()     all plugins started — cross-plugin wiring
    shutdown()  close connections, flush

Two contract checks the kernel aggregates:
    startup_check()   preconditions to serve traffic
    health_check()    runtime dependencies reachable
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from arc.kernel.contracts import CheckResult
from arc.kernel.runtime import Runtime


class Plugin(ABC):
    # ── Identity (required) ────────────────────────────────────────────
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        ...

    # ── Declared graph metadata (class attributes, override as needed) ──
    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    requires_optional: tuple[str, ...] = ()   # soft deps — absence is not fatal
    load_order: int = 100
    critical: bool = False
    description: str = ""

    # ── Synchronous wiring (before the event loop) ─────────────────────
    def setup(self, rt: Runtime) -> None:
        """Register capabilities this plugin provides. Synchronous."""

    def contribute(self, rt: Runtime) -> None:
        """Contribute extensions (routes, cli, schemas). Runs after all setup()."""

    # ── Async lifecycle ────────────────────────────────────────────────
    async def startup(self) -> None:
        """Open connections / warm caches."""

    async def ready(self) -> None:
        """All plugins started; do cross-plugin wiring here."""

    async def shutdown(self) -> None:
        """Close connections / flush buffers."""

    # ── Contract checks ────────────────────────────────────────────────
    async def startup_check(self) -> CheckResult:
        return CheckResult.ok()

    async def health_check(self) -> CheckResult:
        return CheckResult.ok()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<Plugin {self.name} v{self.version}>"