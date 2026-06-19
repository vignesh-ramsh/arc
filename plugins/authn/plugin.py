"""
plugins.authn.plugin
====================
The authn plugin. Critical and ordered after relay (so relay.router /
relay.documents exist) but before business plugins.

  setup()       build AuthConfig (secret from env), bind the auth_service
                singleton to arc + db.session, PROVIDE ``auth.context``.
  contribute()  register the before_req authenticator on the relay registrar,
                force-import document hooks, add the ``arc authn`` CLI group.
                (Routes + schemas are auto-discovered by relay / psqldb.)
"""

from __future__ import annotations

import os

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime

from plugins.authn import auth_service
from plugins.authn.config import AuthConfig, AuthConfigError

log = get_logger("arc.plugin.authn")


class AuthnPlugin(Plugin):
    provides = ("auth.context",)
    requires = ("db.session", "relay.router", "relay.documents")
    load_order = 70                  # after relay (60), before business plugins (100)
    critical = True
    description = "Authentication: stateless JWT + server-side session registry"

    def __init__(self) -> None:
        self._config_error: str | None = None

    @property
    def name(self) -> str:
        return "authn"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── setup: build config, bind the service, provide capability ───────
    def setup(self, rt: Runtime) -> None:
        session_cm = rt.capabilities.require("db.session")
        arc = rt.capabilities.require("relay.documents")
        try:
            config = AuthConfig.from_runtime(rt.plugin_config, os.environ)
        except AuthConfigError as exc:
            self._config_error = str(exc)
            log.error("arc.authn.config_error", error=str(exc))
            return

        auth_service.bind(config, arc, session_cm)          # ← all THREE args
        rt.capabilities.provide("auth.context", instance=auth_service, source=self.name)

    # ── contribute: before_req hook, hooks import, CLI ──────────────────
    def contribute(self, rt: Runtime) -> None:
        if self._config_error:
            return  # nothing to wire; startup_check will abort the boot

        router = rt.capabilities.require("relay.router")
        router.before_req(auth_service.authenticate_request)

        # Ensure @hook("AuthUser", ...) bindings register even if discovery does
        # not scan hooks/ (duplicate registration is de-duplicated by relay).
        from plugins.authn.hooks import users as _users  # noqa: F401

        from plugins.authn.cli import build_cli
        rt.extensions.contribute(Points.CLI_COMMANDS, build_cli(), source=self.name)

    # ── checks ──────────────────────────────────────────────────────────
    async def startup_check(self) -> CheckResult:
        if self._config_error:
            return CheckResult.fail(self._config_error)
        return CheckResult.ok("authn configured")

    async def health_check(self) -> CheckResult:
        return CheckResult.ok("authn ready")
