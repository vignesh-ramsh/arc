"""
plugins.relay.plugin
====================
The relay plugin — an ordinary Arc plugin. It imports no other plugin and
reaches the database only through the ``db.session`` capability.

  setup()       acquire db.session; bind the ``arc`` singleton + read caps /
                base path from config; PROVIDE relay.router (registrar) and
                relay.documents (arc).
  contribute()  run auto-discovery (resources + handler modules), mount the
                relay sub-app under the configured base path, add the
                ``arc relay`` CLI group and a health probe.
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
    load_order = 60          # after db, before business plugins
    critical = True
    description = "Decorator routing + context-bound document API with hooks"

    @property
    def name(self) -> str:
        return "relay"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── setup: bind arc, provide capabilities ───────────────────────────
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
        rt.capabilities.provide("relay.router", instance=_registrar, source=self.name)
        rt.capabilities.provide("relay.documents", instance=_arc, source=self.name)

    # ── contribute: discover, mount, cli, health ────────────────────────
    def contribute(self, rt: Runtime) -> None:
        from starlette.routing import Mount

        from plugins.relay.asgi import RelayASGI
        from plugins.relay.cli import build_cli
        from plugins.relay.discovery import discover

        cfg = dict(rt.plugin_config or {})
        base = str(cfg.get("base_path", "/api/v1")).rstrip("/")
        self._default_rt_limit = int(cfg.get("rate_limit_default", 30))

        roots = self._plugin_roots(rt)
        try:
            discover(_registrar, _arc, roots, base_path=base)
        except Exception as exc:
            # Fail-fast: a bad declaration / route collision must not boot silently.
            log.error("arc.relay.discovery_failed", error=str(exc))
            raise

        asgi = RelayASGI(_registrar, _arc, base_path=base)
        rt.extensions.contribute(Points.HTTP_ROUTES, Mount(base, app=asgi), source=self.name)
        rt.extensions.contribute(Points.CLI_COMMANDS, build_cli(), source=self.name)
        rt.extensions.contribute(Points.HEALTH_CHECKS, self._health_extension, source=self.name)

    def _plugin_roots(self, rt: Runtime) -> list[tuple[str, str]]:
        """Discover (module_prefix, root_dir) pairs to scan.

        module_prefix is the importable Python prefix for handler modules, e.g.
        ``"plugins.hrms"`` so that ``routes/employees.py`` imports as
        ``plugins.hrms.routes.employees``.

        Configurable override via [plugins.relay] discover_roots in arc.toml:
            [plugins.relay]
            discover_roots = {"plugins.hrms" = "plugins/hrms"}

        Otherwise infers from the standard Arc layout:
          • If a ``plugins/`` subdirectory exists at the project root,
            scan its children (standard: plugins/hrms/, plugins/http/, …).
          • Otherwise fall back to scanning the project root directly.

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
            if (child / "resources").is_dir() or (child / "routes").is_dir() \
                    or (child / "api").is_dir():
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