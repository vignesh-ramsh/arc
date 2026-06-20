"""
plugins.redix.queue.worker
==========================
The queue consumer. Runs as a separate process via ``arc queue worker``. It
imports the project's plugin graph (so ``@relay.task`` handlers self-register)
but does NOT bind an HTTP port — it consumes jobs from Redis Streams using a
consumer group (each job delivered to exactly one worker) and dispatches them to
the registered handler.

Retry / backoff / dead-letter:
  • a handler exception increments the job's attempt count and re-enqueues with
    a backoff delay, until ``max_retries`` is exhausted;
  • once exhausted, the job is pushed to ``{prefix}:dead`` with its full failure
    history and its result record marked failed.

Handler lookup goes through relay's registrar (``relay.task_handler(name)``);
the worker never imports business code directly.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any

from arc.kernel.logger import get_logger
from plugins.redix.queue.client import (
    FAILED, PENDING, RUNNING, SUCCESS, QueueClient, _PRIORITIES,
)
from plugins.redix.serializers import decode, encode

log = get_logger("arc.plugin.redix.queue.worker")

_GROUP = "arc-workers"


def _backoff_delay(attempts: int, strategy: str) -> float:
    if strategy == "fixed":
        return 2.0
    # exponential (default): 1, 2, 4, 8, ... capped at 60s
    return float(min(2 ** max(0, attempts - 1), 60))


class QueueWorker:
    def __init__(self, queue: QueueClient, registrar, *,
                 queues: list[str] | None = None, concurrency: int = 1,
                 consumer_name: str | None = None) -> None:
        self._q = queue
        self._reg = registrar
        self._queues = queues or list(_PRIORITIES)
        self._concurrency = max(1, concurrency)
        self._consumer = consumer_name or f"worker-{int(time.time())}"
        self._stop = asyncio.Event()

    async def _ensure_groups(self) -> None:
        """Create the consumer group on each stream (idempotent)."""
        client = self._q._conn.client
        for prio in self._queues:
            key = self._q.stream_key(prio)
            try:
                await client.xgroup_create(key, _GROUP, id="0", mkstream=True)
            except Exception as exc:  # BUSYGROUP if it already exists
                if "BUSYGROUP" not in str(exc):
                    raise

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self._ensure_groups()
        log.info("arc.redix.worker_started", consumer=self._consumer,
                 queues=self._queues, concurrency=self._concurrency)
        sem = asyncio.Semaphore(self._concurrency)
        client = self._q._conn.client
        stream_keys = {self._q.stream_key(p): p for p in self._queues}

        while not self._stop.is_set():
            try:
                # Block up to 2s waiting for new entries across all streams.
                resp = await client.xreadgroup(
                    _GROUP, self._consumer,
                    {k: ">" for k in stream_keys},
                    count=self._concurrency, block=2000,
                )
            except Exception as exc:
                log.error("arc.redix.worker_read_error", error=str(exc))
                await asyncio.sleep(1.0)
                continue

            if not resp:
                continue

            for stream_key, entries in resp:
                for entry_id, fields in entries:
                    await sem.acquire()
                    asyncio.create_task(
                        self._handle(stream_key, entry_id, fields, sem)
                    )

        log.info("arc.redix.worker_stopped", consumer=self._consumer)

    async def _handle(self, stream_key, entry_id, fields, sem: asyncio.Semaphore) -> None:
        client = self._q._conn.client
        try:
            raw = fields.get(b"data") if isinstance(fields, dict) else None
            env = decode(raw) if raw is not None else None
            if not env:
                await client.xack(stream_key, _GROUP, entry_id)
                return

            job_id = env["id"]
            task = env["task"]
            handler = self._reg.task_handler(task)
            if handler is None:
                log.error("arc.redix.task_unknown", task=task, job_id=job_id)
                await self._to_dead(env, "no handler registered for task")
                await client.xack(stream_key, _GROUP, entry_id)
                return

            await self._q._set_result(job_id, {"status": RUNNING, "task": task})
            try:
                result = handler(**env.get("kwargs", {}))
                if asyncio.iscoroutine(result):
                    result = await result
                await self._q._set_result(
                    job_id, {"status": SUCCESS, "task": task, "result": result}
                )
                log.info("arc.redix.task_done", task=task, job_id=job_id)
            except Exception as exc:  # noqa: BLE001 — handler failure
                env["attempts"] = int(env.get("attempts", 0)) + 1
                tb = traceback.format_exc()
                if env["attempts"] > int(env.get("max_retries", 0)):
                    await self._to_dead(env, str(exc), tb)
                    await self._q._set_result(
                        job_id, {"status": FAILED, "task": task, "error": str(exc)}
                    )
                    log.error("arc.redix.task_dead", task=task, job_id=job_id,
                              attempts=env["attempts"])
                else:
                    delay = _backoff_delay(env["attempts"],
                                           env.get("backoff", "exponential"))
                    await asyncio.sleep(delay)
                    await client.xadd(stream_key, {b"data": encode(env)})
                    log.warning("arc.redix.task_retry", task=task, job_id=job_id,
                                attempt=env["attempts"], delay=delay)
            finally:
                await client.xack(stream_key, _GROUP, entry_id)
        finally:
            sem.release()

    async def _to_dead(self, env: dict, error: str, tb: str | None = None) -> None:
        env = dict(env)
        env["failed_at"] = time.time()
        env["error"] = error
        if tb:
            env["traceback"] = tb
        await self._q._conn.client.rpush(self._q._dead_key(), encode(env))