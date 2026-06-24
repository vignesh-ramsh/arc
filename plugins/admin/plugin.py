"""
plugins.admin.plugin
===================
The admin plugin. Critical-optional; ordered after authn so ``auth.context``
exists. It does two things:

  setup()       bind admin_ctx to the auth.context capability + read config
                (project root, migrate command, row block list).
  contribute()  mount the single-page UI at ``/admin`` (static files) and add a
                health probe. The ``/api/v1/admin/*`` JSON routes are plain relay
                routes in routes/*.py — relay auto-discovers and mounts them, so
                there is nothing to register for the API here.

Config ([plugins.admin] in arc.toml, all optional):
    migrate_command = ["arc", "db", "migrate"]   # how the Migrate button runs
    row_blocklist   = ["AuthUser", "AuthSession"] # tables the Row Editor refuses
    project_root    = "."                         # defaults to the process CWD
"""

from __future__ import annotations

from pathlib import Path

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime

from plugins.admin import admin_ctx

log = get_logger("arc.plugin.admin")

_DEFAULT_MIGRATE_CMD = ["arc", "psqldb", "migrate"]
_DEFAULT_ROW_BLOCKLIST = ("AuthUser", "AuthSession")


class AdminPlugin(Plugin):
    provides = ("admin.panel",)
    requires = ("db.session", "relay.router", "relay.documents", "auth.context")
    requires_optional = ("queue.client",)   # redix; Queue panel degrades if absent
    load_order = 95                  # after authn (70) and business plugins
    critical = False
    description = "Superuser admin panel: tables, users, queue jobs, rows"

    @property
    def name(self) -> str:
        return "admin"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── setup: bind the context holder ──────────────────────────────────
    def setup(self, rt: Runtime) -> None:
        auth = rt.capabilities.require("auth.context")
        cfg = dict(rt.plugin_config or {})

        root_cfg = cfg.get("project_root")
        project_root = Path(root_cfg).resolve() if root_cfg else Path.cwd()

        migrate_cmd = cfg.get("migrate_command") or _DEFAULT_MIGRATE_CMD
        if not isinstance(migrate_cmd, (list, tuple)) or not migrate_cmd:
            log.warning("arc.admin.bad_migrate_command", value=migrate_cmd)
            migrate_cmd = _DEFAULT_MIGRATE_CMD

        blocklist = cfg.get("row_blocklist")
        if not isinstance(blocklist, (list, tuple)):
            blocklist = _DEFAULT_ROW_BLOCKLIST
        row_blocklist = frozenset(str(t) for t in blocklist)

        # Optional: redix queue.client. None when redix is absent — the Queue
        # panel handles that by showing an 'unavailable' state.
        queue = rt.capabilities.get("queue.client")

        admin_ctx.bind(
            auth=auth,
            project_root=project_root,
            migrate_cmd=list(migrate_cmd),
            row_blocklist=row_blocklist,
            queue=queue,
        )
        rt.capabilities.provide("admin.panel", instance=admin_ctx, source=self.name)
        log.info("arc.admin.bound", project_root=str(project_root),
                 migrate_cmd=list(migrate_cmd), queue=queue is not None)

    # ── contribute: mount the SPA + health ──────────────────────────────
    def contribute(self, rt: Runtime) -> None:
        # Note: route modules (routes/*.py) are auto-discovered and imported
        # by relay's discovery pass. The sys.path fix in __init__.py ensures
        # relay can resolve "admin.routes.*" as a top-level package.
        ui_dir = Path(__file__).parent / "ui"
        if ui_dir.is_dir() and any(ui_dir.iterdir()):
            from starlette.routing import Mount
            from starlette.staticfiles import StaticFiles
            rt.extensions.contribute(
                Points.HTTP_ROUTES,
                Mount("/admin", app=StaticFiles(directory=str(ui_dir), html=True)),
                source=self.name,
            )
            log.info("arc.admin.ui_mounted", path="/admin", dir=str(ui_dir))
        else:
            log.warning("arc.admin.ui_missing", dir=str(ui_dir),
                        detail="UI not mounted; build the SPA into plugins/admin/ui/")

        rt.extensions.contribute(Points.HEALTH_CHECKS, self._health, source=self.name)

    async def _health(self) -> CheckResult:
        ui_dir = Path(__file__).parent / "ui"
        ui = "ui mounted" if (ui_dir.is_dir() and any(ui_dir.iterdir())) else "ui missing"
        return CheckResult.ok(f"admin ready ({ui})")

    async def health_check(self) -> CheckResult:
        return CheckResult.ok("admin ready")


__all__ = ["AdminPlugin"]