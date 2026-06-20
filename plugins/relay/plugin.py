"""
plugins.relay.plugin
====================
The relay plugin — an ordinary Arc plugin. It imports no other plugin and
reaches the database only through the ``db.session`` capability. It softly
depends on redix (cache.client / queue.client / scheduler.client) and degrades
gracefully when redix is absent.

  setup()       acquire db.session (hard) + the three redix capabilities
                (optional, via rt.capabilities.get); bind the ``arc`` singleton;
                PROVIDE relay.router and relay.documents.
  contribute()  run discovery (custom handler modules), mount the relay sub-app,
                CONTRIBUTE the flat arc.* surface (document API + cache/queue/
                scheduler facade + streaming bulk ops) to Points.ARC_SURFACE,
                add the ``arc relay`` CLI and a health probe.

Note: declarative resource auto-CRUD has been removed — all endpoints are custom
handlers in routes/.
"""

from __future__ import annotations

from pathlib import Path

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime

from plugins.relay import arc as _arc, relay as _registrar

log = get_logger("arc.plugin.relay")


class RelayPlugin(Plugin):
    provides = ("relay.router", "relay.documents")
    requires = ("db.session",)
    requires_optional = ("cache.client", "queue.client", "scheduler.client")
    load_order = 60          # after db (and redix at 40), before business plugins
    critical = True
    description = "Decorator routing + context-bound document API with hooks"

    def __init__(self) -> None:
        self._cache_cap = None
        self._queue_cap = None
        self._scheduler_cap = None
        self._lru_max = 1000

    @property
    def name(self) -> str:
        return "relay"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── setup: bind arc, acquire caps, provide capabilities ─────────────
    def setup(self, rt: Runtime) -> None:
        session_cm = rt.capabilities.require("db.session")
        cfg = dict(rt.plugin_config or {})
        _arc._bind(
            session_cm, _registrar,
            list_cap=int(cfg.get("list_cap", 1000)),
            rm_many_cap=int(cfg.get("rm_many_cap", 1000)),
            max_bulk_rows=int(cfg.get("max_bulk_rows", 1000)),
            max_body_bytes=int(cfg.get("max_body_bytes", 1_048_576)),
            type_ttl=float(cfg.get("type_ttl", 60.0)),
        )
        # Optional redix capabilities — any may be None (redix absent). Stored
        # for use when building the arc.* facade in contribute().
        self._cache_cap = rt.capabilities.get("cache.client")
        self._queue_cap = rt.capabilities.get("queue.client")
        self._scheduler_cap = rt.capabilities.get("scheduler.client")
        self._lru_max = int(cfg.get("cache_fallback_max_entries", 1000))

        rt.capabilities.provide("relay.router", instance=_registrar, source=self.name)
        rt.capabilities.provide("relay.documents", instance=_arc, source=self.name)

    # ── contribute: discover, mount, arc surface, cli, health ───────────
    def contribute(self, rt: Runtime) -> None:
        from starlette.routing import Mount

        from plugins.relay.asgi import RelayASGI
        from plugins.relay.cli import build_cli
        from plugins.relay.discovery import discover
        from plugins.relay.redix_facade import build_facade
        from plugins.relay.streaming import build_streaming

        cfg = dict(rt.plugin_config or {})
        base = str(cfg.get("base_path", "/api/v1")).rstrip("/")
        self._default_rt_limit = int(cfg.get("rate_limit_default", 30))

        roots = self._plugin_roots(rt)
        try:
            discover(_registrar, _arc, roots, base_path=base)
        except Exception as exc:
            # Fail-fast: a handler module that fails to import must not boot silently.
            log.error("arc.relay.discovery_failed", error=str(exc))
            raise

        asgi = RelayASGI(_registrar, _arc, base_path=base)
        rt.extensions.contribute(Points.HTTP_ROUTES, Mount(base, app=asgi), source=self.name)
        rt.extensions.contribute(Points.CLI_COMMANDS, build_cli(), source=self.name)
        rt.extensions.contribute(Points.HEALTH_CHECKS, self._health_extension, source=self.name)

        # Build and contribute the flat arc.* surface.
        surface = self._build_arc_surface(
            build_facade=build_facade, build_streaming=build_streaming
        )
        rt.extensions.contribute(Points.ARC_SURFACE, surface, source=self.name)

    # ── arc.* surface assembly ──────────────────────────────────────────
    def _build_arc_surface(self, *, build_facade, build_streaming) -> dict:
        """Collect relay's contributions to the flat ``arc`` object: the document
        API methods + the cache/queue/scheduler facade + streaming bulk ops."""
        # Document API methods (bound methods of the arc singleton). Exposed on
        # the flat surface so business plugins call `arc.list(...)` after
        # `import arc`. Only public, callable attributes are surfaced.
        doc_methods = {}
        for attr in (
            "get", "list", "list_page", "count", "exists", "aggregate",
            "save", "update", "save_many", "update_many",
            "rm", "rm_many", "query", "tx",
        ):
            fn = getattr(_arc, attr, None)
            if callable(fn):
                doc_methods[attr] = fn

        facade = build_facade(
            _arc, _registrar,
            cache_cap=self._cache_cap,
            queue_cap=self._queue_cap,
            scheduler_cap=self._scheduler_cap,
            lru_max_entries=self._lru_max,
        )
        streaming = build_streaming(_arc)

        # Merge; relay owns all of these so there is no intra-relay collision.
        surface: dict = {}
        surface.update(doc_methods)
        surface.update(facade)
        surface.update(streaming)
        return surface

    def _plugin_roots(self, rt: Runtime) -> list[tuple[str, str]]:
        """Discover (module_prefix, root_dir) pairs to scan.

        A plugin is scanned only if it has a ``routes/`` or ``api/`` directory
        (the only sources relay imports now that resource auto-CRUD is gone).
        relay itself is always excluded from its own discovery.
        """
        cfg = dict(rt.plugin_config or {})
        override = cfg.get("discover_roots")
        if override:
            return [(name, path) for name, path in override.items()]

        roots: list[tuple[str, str]] = []
        project = Path.cwd()

        plugins_dir = project / "plugins"
        if plugins_dir.is_dir():
            scan_dir = plugins_dir
            module_prefix = "plugins"
        else:
            scan_dir = project
            module_prefix = ""

        for child in sorted(scan_dir.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            if child.name == self.name:   # never discover relay itself
                continue
            if (child / "routes").is_dir() or (child / "api").is_dir():
                prefix = f"{module_prefix}.{child.name}" if module_prefix else child.name
                roots.append((prefix, str(child)))
        return roots

    async def _health_extension(self) -> CheckResult:
        return CheckResult.ok(
            f"relay: {len(_registrar.routes)} routes, "
            f"{len(_registrar.hook_summary())} hook bindings"
        )

    async def health_check(self) -> CheckResult:
        return CheckResult.ok("relay ready")


__all__ = ["RelayPlugin"]