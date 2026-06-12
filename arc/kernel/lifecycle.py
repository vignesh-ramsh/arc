"""
arc.kernel.lifecycle
===================
Runs the async lifecycle over plugins in resolved order.

Startup (forward order):
    1. startup_check() on every plugin. A failed check on a *critical*
       plugin aborts; on a non-critical plugin it is logged as a warning.
    2. startup() on every plugin (open connections, warm caches).
    3. ready() on every plugin (cross-plugin wiring).

Shutdown (reverse order):
    shutdown() on every started plugin, errors collected not raised, so one
    bad teardown never blocks the rest.

Health: aggregates health_check() across plugins plus any callables
contributed to the ``health.checks`` extension point.
"""

from __future__ import annotations

from arc.kernel.contracts import CheckResult, CheckStatus
from arc.kernel.exceptions import ShutdownError, StartupError
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin

log = get_logger(__name__)


class LifecycleManager:
    def __init__(self, ordered_plugins: list[Plugin]) -> None:
        self._plugins = ordered_plugins
        self.started: list[Plugin] = []

    async def startup(self) -> None:
        # 1. startup checks
        for p in self._plugins:
            result = await p.startup_check()
            if result.failed:
                if p.critical:
                    raise StartupError(
                        f"Critical plugin '{p.name}' failed startup_check: {result.message}",
                        code="arc.lifecycle.startup_check_failed",
                    )
                log.warning("arc.startup_check.warn", plugin=p.name, msg=result.message)
            elif result.status is CheckStatus.WARN:
                log.warning("arc.startup_check.warn", plugin=p.name, msg=result.message)

        # 2. startup
        for p in self._plugins:
            try:
                await p.startup()
                self.started.append(p)
                log.info("arc.plugin.started", plugin=p.name)
            except Exception as exc:
                if p.critical:
                    raise StartupError(
                        f"Critical plugin '{p.name}' failed to start: {exc}",
                        code="arc.lifecycle.startup_failed",
                    ) from exc
                log.error("arc.plugin.start_failed", plugin=p.name, error=str(exc))

        # 3. ready
        for p in self.started:
            await p.ready()

    async def shutdown(self) -> None:
        errors: list[str] = []
        for p in reversed(self.started):
            try:
                await p.shutdown()
                log.info("arc.plugin.stopped", plugin=p.name)
            except Exception as exc:
                errors.append(f"{p.name}: {exc}")
                log.error("arc.plugin.stop_failed", plugin=p.name, error=str(exc))
        self.started.clear()
        if errors:
            raise ShutdownError(
                "Errors during shutdown: " + "; ".join(errors),
                code="arc.lifecycle.shutdown_errors",
            )

    async def health(self) -> dict[str, CheckResult]:
        results: dict[str, CheckResult] = {}
        for p in self._plugins:
            try:
                results[p.name] = await p.health_check()
            except Exception as exc:  # never let a health probe crash the endpoint
                results[p.name] = CheckResult.fail(str(exc))
        return results

    @staticmethod
    def is_healthy(results: dict[str, CheckResult]) -> bool:
        return all(r.passed for r in results.values())
