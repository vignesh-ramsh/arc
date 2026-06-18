"""Arc plugin: sales"""
from __future__ import annotations

from arc.kernel.contracts import CheckResult
from arc.kernel.plugin import Plugin
from arc.kernel.runtime import Runtime


class SalesPlugin(Plugin):
    """Sales plugin.

    Auto-discovered by the framework:
      sales/schemas/{Customer,Product,Order}.json  → tables (psqldb)
      sales/resources/{order,product}.json         → auto-CRUD routes (relay)
      sales/routes/orders.py                       → custom routes + hooks (relay)

    contribute() is intentionally empty — relay/psqldb handle discovery.
    """

    requires = ("db.session", "relay.router", "relay.documents")
    load_order = 100

    @property
    def name(self) -> str:
        return "sales"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def health_check(self) -> CheckResult:
        return CheckResult.ok("sales ready")


__all__ = ["SalesPlugin"]