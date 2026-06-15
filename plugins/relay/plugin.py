"""
arc.plugins.relay.plugin
=======================
The relay plugin. It is an ordinary Arc plugin: it imports no other plugin and
reaches the database only through the ``db.session`` capability.

Wiring:
  setup()       acquire db.session; build the DocumentGateway; PROVIDE
                ``relay.router`` (the registrar) and ``relay.documents`` (the
                gateway) so other plugins can require them in their contribute().
  contribute()  mount the relay sub-app into ``http.routes`` (order-independent,
                see asgi.RelayASGI) and add an ``arc relay`` CLI group.

Because relay.router is provided in setup(), and contribute() runs after EVERY
plugin's setup(), business plugins can do this with zero imports:

    def contribute(self, rt):
        relay = rt.capabilities.require("relay.router")

        @relay.post("/employees")
        async def create(ctx):
            return await ctx.documents.insert("Employee", ctx.data)

        @relay.hook("Employee", "validate")
        async def must_have_code(doc):
            if not doc.get("employee_code"):
                doc.fail("employee_code is required", field="employee_code")
"""

from __future__ import annotations

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime
from plugins.relay.documents import DocumentGateway

log = get_logger("arc.plugin.relay")


class RelayPlugin(Plugin):
    provides = ("relay.router", "relay.documents")
    requires = ("db.session",)
    load_order = 60          # after db/api, before business plugins
    critical = True
    description = "Decorator routing + document-event pipeline (hooks for integrity)"

    def __init__(self) -> None:
        # Use the package-level singleton so module-level decorators
        # (from plugins.relay import route, hook) and the capability
        # registrar (rt.capabilities.require("relay.router")) are the SAME object.
        from plugins.relay import relay as _registrar

        self._registrar = _registrar
        self._gateway: DocumentGateway | None = None

    @property
    def name(self) -> str:
        return "relay"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── setup: provide registrar + gateway (db.session already resolved) ─
    def setup(self, rt: Runtime) -> None:
        session_cm = rt.capabilities.require("db.session")
        self._gateway = DocumentGateway(session_cm, self._registrar)
        rt.capabilities.provide("relay.router", instance=self._registrar, source=self.name)
        rt.capabilities.provide("relay.documents", instance=self._gateway, source=self.name)

    # ── contribute: mount routes + cli + health ─────────────────────────
    def contribute(self, rt: Runtime) -> None:
        from plugins.relay.asgi import RelayASGI
        from starlette.routing import Mount

        prefix = str(rt.plugin_config.get("prefix", "")).rstrip("/")
        asgi = RelayASGI(self._registrar, self._gateway)
        rt.extensions.contribute(Points.HTTP_ROUTES, Mount(prefix, app=asgi), source=self.name)

        from plugins.relay.cli import build_cli

        rt.extensions.contribute(Points.CLI_COMMANDS, build_cli(), source=self.name)
        rt.extensions.contribute(Points.HEALTH_CHECKS, self._health_extension, source=self.name)

    async def _health_extension(self) -> CheckResult:
        return CheckResult.ok(
            f"relay: {len(self._registrar.routes)} routes, "
            f"{len(self._registrar.hook_summary())} hook bindings"
        )

    async def health_check(self) -> CheckResult:
        return CheckResult.ok("relay ready")