"""
plugins.redix.scheduler.client
==============================
The ``scheduler.client`` capability. Registration + introspection only; the tick
loop that fires due jobs lives in ``redix.scheduler.worker`` and runs as a
separate ``arc scheduler worker`` process (fixed-leader).

Schedules are stored in a Redis hash so every process (web + worker) sees the
same set. The schedule *handlers* are declared via ``@relay.scheduled`` on
relay's registrar; this client stores only the timing spec keyed by name.

A due job is DISPATCHED onto the queue (``queue.enqueue``) rather than run
inline, so scheduled jobs inherit the queue's retry/dead-letter handling.
"""

from __future__ import annotations

import time
from typing import Any

from arc.kernel.logger import get_logger
from plugins.redix.connection import RedisConnection
from plugins.redix.keys import KeyBuilder
from plugins.redix.serializers import decode, encode

log = get_logger("arc.plugin.redix.scheduler")


class SchedulerClient:
    def __init__(self, conn: RedisConnection, *, key_prefix: str = "arc:sched",
                 leader_id: str = "scheduler-primary") -> None:
        self._conn = conn
        self._keys = KeyBuilder(key_prefix)
        self._leader_id = leader_id

    @property
    def leader_id(self) -> str:
        return self._leader_id

    def _schedules_key(self) -> str:
        return self._keys.build("schedules")

    def _history_key(self, name: str) -> str:
        return self._keys.build("history", name)

    def _lock_key(self, name: str) -> str:
        return self._keys.build("lock", name)

    # ── registration ────────────────────────────────────────────────────
    async def register_cron(self, name: str, expr: str, **opts: Any) -> None:
        spec = {"name": name, "kind": "cron", "expr": expr, **opts}
        await self._conn.client.hset(self._schedules_key(), name, encode(spec))
        log.info("arc.redix.schedule_registered", name=name, kind="cron", expr=expr)

    async def register_every(self, name: str, *, seconds: int, **opts: Any) -> None:
        spec = {"name": name, "kind": "every", "seconds": seconds, **opts}
        await self._conn.client.hset(self._schedules_key(), name, encode(spec))
        log.info("arc.redix.schedule_registered", name=name, kind="every",
                 seconds=seconds)

    async def unregister(self, name: str) -> None:
        await self._conn.client.hdel(self._schedules_key(), name)

    # ── introspection ───────────────────────────────────────────────────
    async def list(self) -> list[dict]:
        raw = await self._conn.client.hgetall(self._schedules_key())
        return [decode(v) for v in raw.values()]

    async def get(self, name: str) -> dict | None:
        raw = await self._conn.client.hget(self._schedules_key(), name)
        return decode(raw)

    async def history(self, name: str, *, limit: int = 20) -> list[dict]:
        raw_items = await self._conn.client.lrange(self._history_key(name), 0, limit - 1)
        return [decode(r) for r in raw_items]

    async def record_run(self, name: str, *, job_id: str | None,
                         dispatched_at: float | None = None) -> None:
        entry = {"name": name, "job_id": job_id,
                 "dispatched_at": dispatched_at or time.time()}
        client = self._conn.client
        await client.lpush(self._history_key(name), encode(entry))
        await client.ltrim(self._history_key(name), 0, 199)  # keep last 200

    async def ping(self) -> bool:
        return await self._conn.ping()