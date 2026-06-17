"""Arc plugin: hrms"""
from __future__ import annotations

from arc.kernel.contracts import CheckResult
from arc.kernel.plugin import Plugin
from arc.kernel.runtime import Runtime


class HrmsPlugin(Plugin):
    """Human Resource Management plugin.

    Relay auto-discovers routes and hooks from:
      hrms/resources/employee.json   → auto-CRUD at /api/v1/hrms/employees
      hrms/routes/employees.py       → custom routes + all hooks

    This class does nothing in contribute() — that is intentional.
    The relay plugin's discover() handles everything.
    """

    requires   = ("db.session", "relay.router", "relay.documents")
    load_order = 100

    @property
    def name(self) -> str:
        return "hrms"

    @property
    def version(self) -> str:
        return "1.0.0"

    # contribute() is intentionally empty.
    # Schema sources → auto-discovered by psqldb (reads schemas/ and patches/).
    # Routes + hooks → auto-discovered by relay (reads resources/*.json and routes/*.py).

    async def health_check(self) -> CheckResult:
        return CheckResult.ok("hrms ready")