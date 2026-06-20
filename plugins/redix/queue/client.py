"""
plugins.redix.queue.client
===========================
The ``queue.client`` capability. Enqueue side only — the consume loop lives in
``redix.queue.worker`` and runs as a separate ``arc queue worker`` process.

Transport: Redis Streams (``XADD`` to enqueue, consumer groups in the worker so
each job is delivered to exactly one consumer). Job status/result is stored in a
TTL'd key so ``arc.queue_result(job_id)`` can poll it.

Task *handlers* are NOT registered here — they are declared via ``@relay.task``
on relay's registrar and looked up by the worker. This client only moves
serialized job envelopes; it never imports handler code.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from arc.kernel.logger import get_logger
from plugins.redix.connection import RedisConnection
from plugins.redix.keys import KeyBuilder
from plugins.redix.serializers import decode, encode

log = get_logger("arc.plugin.redix.queue")

# Job result lifecycle states.
PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"

_PRIORITIES = ("high", "default", "low")


class QueueClient:
    def __init__(self, conn: RedisConnection, *, key_prefix: str = "arc:queue",
                 default_retries: int = 3, result_ttl: int = 86400) -> None:
        self._conn = conn
        self._keys = KeyBuilder(key_prefix)
        self._default_retries = default_retries
        self._result_ttl = result_ttl

    # ── key helpers ─────────────────────────────────────────────────────
    def stream_key(self, priority: str = "default") -> str:
        if priority not in _PRIORITIES:
            priority = "default"
        return self._keys.build("stream", priority)

    def _result_key(self, job_id: str) -> str:
        return self._keys.build("result", job_id)

    def _dead_key(self) -> str:
        return self._keys.build("dead")

    # ── enqueue ─────────────────────────────────────────────────────────
    async def enqueue(self, task: str, *, priority: str = "default",
                      max_retries: int | None = None, **kwargs: Any) -> str:
        """Append a job to the stream and seed its result record as pending.
        Returns the job id."""
        job_id = str(uuid.uuid4())
        retries = self._default_retries if max_retries is None else max_retries
        envelope = {
            "id": job_id,
            "task": task,
            "kwargs": kwargs,
            "priority": priority,
            "max_retries": retries,
            "attempts": 0,
            "enqueued_at": time.time(),
        }
        client = self._conn.client
        # The stream stores a single field "data" holding the JSON envelope.
        await client.xadd(self.stream_key(priority), {b"data": encode(envelope)})
        await self._set_result(job_id, {"status": PENDING, "task": task})
        log.info("arc.redix.enqueued", task=task, job_id=job_id, priority=priority)
        return job_id

    # ── result tracking ─────────────────────────────────────────────────
    async def _set_result(self, job_id: str, payload: dict) -> None:
        await self._conn.client.set(
            self._result_key(job_id), encode(payload), ex=self._result_ttl
        )

    async def result(self, job_id: str) -> dict | None:
        """Return the job's status record, or None if unknown/expired.

        Shape: ``{"status": pending|running|success|failed, "task": ...,
        "result": <any>?, "error": <str>?}``.
        """
        raw = await self._conn.client.get(self._result_key(job_id))
        return decode(raw)

    # ── dead-letter inspection ──────────────────────────────────────────
    async def dead_list(self, *, limit: int = 50) -> list[dict]:
        raw_items = await self._conn.client.lrange(self._dead_key(), 0, limit - 1)
        return [decode(r) for r in raw_items]

    async def dead_count(self) -> int:
        return int(await self._conn.client.llen(self._dead_key()))

    async def dead_purge(self) -> int:
        n = await self.dead_count()
        await self._conn.client.delete(self._dead_key())
        return n

    async def dead_retry(self, job_id: str) -> bool:
        """Move a single dead job back onto its stream. Returns True if found."""
        client = self._conn.client
        items = await client.lrange(self._dead_key(), 0, -1)
        for raw in items:
            env = decode(raw)
            if env.get("id") == job_id:
                env["attempts"] = 0
                await client.xadd(
                    self.stream_key(env.get("priority", "default")),
                    {b"data": encode(env)},
                )
                await client.lrem(self._dead_key(), 1, raw)
                await self._set_result(job_id, {"status": PENDING,
                                                "task": env.get("task")})
                return True
        return False

    # ── status counts (for `arc queue status`) ──────────────────────────
    async def status(self) -> dict:
        client = self._conn.client
        out: dict[str, Any] = {"streams": {}, "dead": await self.dead_count()}
        for prio in _PRIORITIES:
            try:
                out["streams"][prio] = int(await client.xlen(self.stream_key(prio)))
            except Exception:
                out["streams"][prio] = 0
        return out

    async def ping(self) -> bool:
        return await self._conn.ping()