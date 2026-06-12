"""
arc.plugins.api.plugin
====================
``ApiPlugin`` — the bundled REST layer. It ``requires "db.session"`` and
contributes routes to the ``http.routes`` extension point. It does NOT import
the http plugin and the http plugin does not import it — they meet only at the
extension point. The kernel never mentions either by name.

Resource discovery is itself extension-driven: any plugin can drop Resource
declarations in ``{plugin}/resources/*.py`` (auto-discovered) or contribute
them to ``resource.sources`` directly. api treats both identically.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime
from arc.plugins.api.crud import build_routes
from arc.plugins.api.custom import api as custom_api
from arc.plugins.api.resource import Resource

log = get_logger(__name__)


class ApiPlugin(Plugin):
    provides = ("http.router",)
    requires = ("db.session",)
    load_order = 50
    critical = True
    description = "REST layer — auto-CRUD + custom routes"

    @property
    def name(self) -> str:
        return "api"

    @property
    def version(self) -> str:
        return "1.0.0"

    def setup(self, rt: Runtime) -> None:
        # A marker capability so other plugins can detect REST is present.
        rt.capabilities.provide("http.router", instance=self, source=self.name)

    def contribute(self, rt: Runtime) -> None:
        session_cm = rt.capabilities.require("db.session")

        # 1. Resources: from the extension point + auto-discovered dirs.
        resources: list[Resource] = list(rt.extensions.get(Points.RESOURCE_SOURCES))
        resources.extend(self._discover_resources())

        route_count = 0
        for resource in resources:
            for route in build_routes(resource, session_cm):
                rt.extensions.contribute(Points.HTTP_ROUTES, route, source=self.name)
                route_count += 1

        # 2. Custom routes registered via @api.* decorators.
        from starlette.routing import Route

        for cr in custom_api.routes():
            rt.extensions.contribute(
                Points.HTTP_ROUTES,
                Route(cr.path, cr.handler, methods=[cr.method]),
                source=self.name,
            )
            route_count += 1

        # 3. The `arc api` CLI group.
        from arc.plugins.api.cli import build_cli

        rt.extensions.contribute(Points.CLI_COMMANDS, build_cli(), source=self.name)
        log.info("arc.api.routes_contributed", count=route_count, resources=len(resources))

    def _discover_resources(self) -> list[Resource]:
        """Import {plugin}.resources.* and collect module-level Resource instances."""
        from arc.kernel.loader import LockFile, find_lock_file

        found: list[Resource] = []
        try:
            lock_path = find_lock_file()
        except Exception:
            return found
        root = lock_path.parent
        try:
            lock = LockFile.model_validate(json.loads(lock_path.read_text("utf-8")))
        except Exception:
            return found

        for entry in lock.plugins:
            res_dir = root / entry.name / "resources"
            if not res_dir.is_dir():
                continue
            for py in sorted(res_dir.glob("*.py")):
                if py.stem == "__init__":
                    continue
                module_name = f"{entry.name}.resources.{py.stem}"
                try:
                    module = importlib.import_module(module_name)
                except Exception as exc:
                    log.error("arc.api.resource_import_failed", module=module_name, error=str(exc))
                    continue
                for value in vars(module).values():
                    if isinstance(value, Resource):
                        found.append(value)
        return found

    async def health_check(self) -> CheckResult:
        return CheckResult.ok("api ready")
