"""
plugins.redix.plugin
===================
``RedixPlugin`` — provides cache.client, queue.client, scheduler.client from one
shared Redis pool. ``critical = false``: the web process should still boot if
Redis is briefly unreachable (relay degrades to its fallbacks). The worker
processes treat Redis as load-bearing and fail their own startup_check if it is
unreachable.

  setup()       build config; construct the connection + three clients;
                PROVIDE the three capabilities (as the client instances).
  contribute()  add the ``arc cache`` / ``arc queue`` / ``arc scheduler`` CLI.
  startup()     open the Redis pool; bind the redix singletons.
  shutdown()    close the pool.
"""

from __future__ import annotations

import os

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime

from plugins.redix import _bind
from plugins.redix.cache.client import CacheClient
from plugins.redix.config import RedixConfig
from plugins.redix.connection import RedisConnection
from plugins.redix.queue.client import QueueClient
from plugins.redix.scheduler.client import SchedulerClient

log = get_logger("arc.plugin.redix")


class RedixPlugin(Plugin):
    provides = ("cache.client", "queue.client", "scheduler.client")
    requires = ()
    load_order = 40          # early — before relay (60), so relay can opt in
    critical = False         # absence / brief unavailability must not abort boot
    description = "Redis-backed cache, queue, and scheduler (one pool, 3 capabilities)"

    def __init__(self) -> None:
        self._cfg: RedixConfig | None = None
        self._conn: RedisConnection | None = None
        self._cache: CacheClient | None = None
        self._queue: QueueClient | None = None
        self._scheduler: SchedulerClient | None = None

    @property
    def name(self) -> str:
        return "redix"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── setup: build clients, provide capabilities ──────────────────────
    def setup(self, rt: Runtime) -> None:
        self._cfg = RedixConfig.from_mapping(rt.plugin_config, os.environ)
        self._conn = RedisConnection(
            self._cfg.url, max_connections=self._cfg.max_connections
        )
        self._cache = CacheClient(
            self._conn,
            key_prefix=self._cfg.cache.key_prefix,
            default_ttl=self._cfg.cache.default_ttl,
        )
        self._queue = QueueClient(
            self._conn,
            key_prefix=self._cfg.queue.key_prefix,
            default_retries=self._cfg.queue.default_retries,
            result_ttl=self._cfg.queue.result_ttl,
        )
        self._scheduler = SchedulerClient(
            self._conn,
            key_prefix=self._cfg.scheduler.key_prefix,
            leader_id=self._cfg.scheduler.leader_id,
        )
        rt.capabilities.provide("cache.client", instance=self._cache, source=self.name)
        rt.capabilities.provide("queue.client", instance=self._queue, source=self.name)
        rt.capabilities.provide("scheduler.client", instance=self._scheduler,
                                source=self.name)

    # ── contribute: CLI ─────────────────────────────────────────────────
    def contribute(self, rt: Runtime) -> None:
        from plugins.redix.cli import build_cli

        # build_cli() returns several Typer groups (cache/queue/scheduler/
        # schedule); contribute each separately so the kernel's CLI mounter
        # (which expects individual Typer objects) picks them all up.
        for group in build_cli():
            rt.extensions.contribute(Points.CLI_COMMANDS, group, source=self.name)

    # ── lifecycle ───────────────────────────────────────────────────────
    async def startup(self) -> None:
        assert self._conn is not None
        try:
            await self._conn.connect()
        except Exception as exc:  # noqa: BLE001 — non-critical: log, don't abort
            log.warning("arc.redix.startup_degraded", error=str(exc),
                        detail="Redis unreachable at startup; consumers will "
                               "fall back until it recovers")
        _bind(self._conn, self._cache, self._queue, self._scheduler)

    async def shutdown(self) -> None:
        if self._conn is not None:
            await self._conn.disconnect()

    # ── checks ──────────────────────────────────────────────────────────
    async def startup_check(self) -> CheckResult:
        # Non-critical plugin: report but never fail the build on connectivity.
        if self._cfg is None:
            return CheckResult.fail("redix config not built.")
        return CheckResult.ok("redix configured")

    async def health_check(self) -> CheckResult:
        if self._conn is None:
            return CheckResult.fail("redix not initialised")
        ok = await self._conn.ping()
        return CheckResult.ok("redis reachable") if ok else \
            CheckResult.fail("redis unreachable")


__all__ = ["RedixPlugin"]