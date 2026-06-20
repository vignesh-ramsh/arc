"""
plugins.relay.redix_facade
==========================
The cache / queue / scheduler methods exposed on the flat ``arc`` surface, plus
their graceful fallbacks when redix (cache.client / queue.client /
scheduler.client) is absent or a call fails at runtime.

relay softly depends on redix (``requires_optional``). At setup it acquires the
three optional capabilities with ``rt.capabilities.get(...)`` — any may be
None. This module turns "maybe a capability, maybe None" into always-callable
``arc.*`` methods:

  cache    → redix cache.client, else a bounded in-process LRU (per-worker,
             best-effort, NOT coherent across workers). Logged once on first
             fallback use, then rate-limited.
  queue    → redix queue.client, else run the task inline via Starlette
             BackgroundTask. No retry / dead-letter / persistence in fallback;
             logged loudly on first use. queue_result returns None in fallback.
  schedule → redix scheduler.client, else registration succeeds but automatic
             dispatch does NOT happen; a one-time warning tells the operator to
             run ``arc schedule export`` and install system cron.

``build_facade(...)`` returns the ``{attr: callable}`` dict relay contributes to
ARC_SURFACE.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Callable

from arc.kernel.logger import get_logger

log = get_logger("arc.plugin.relay.redix_facade")


# ── bounded, TTL-aware in-process LRU (cache fallback) ───────────────────────

class _LRUCache:
    """Tiny bounded LRU with per-entry TTL. Per-worker, best-effort. Not a real
    cache — just a degraded stand-in so arc.get_cache/set_cache keep working
    when redix is unavailable."""

    def __init__(self, max_entries: int = 1000) -> None:
        self._max = max(1, max_entries)
        self._d: "OrderedDict[str, tuple[float | None, Any]]" = OrderedDict()

    def get(self, key: str) -> Any:
        item = self._d.get(key)
        if item is None:
            return None
        expires, value = item
        if expires is not None and expires < time.time():
            self._d.pop(key, None)
            return None
        self._d.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: int | None) -> None:
        expires = (time.time() + ttl) if ttl and ttl > 0 else None
        self._d[key] = (expires, value)
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)

    def delete(self, key: str) -> None:
        self._d.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        victims = [k for k in self._d if k.startswith(prefix)]
        for k in victims:
            self._d.pop(k, None)
        return len(victims)


class _OnceLogger:
    """Logs a given event key at most once per process, then stays quiet (so a
    sustained outage doesn't flood logs)."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def warn_once(self, key: str, event: str, **fields: Any) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        log.warning(event, **fields)


def build_facade(arc, registrar, *, cache_cap, queue_cap, scheduler_cap,
                 lru_max_entries: int = 1000) -> dict[str, Callable]:
    """Return the ``{attr: callable}`` map relay contributes to ARC_SURFACE.

    *cache_cap* / *queue_cap* / *scheduler_cap* are the optional capabilities
    (may be None). *registrar* is relay's Relay (for task handler lookup in the
    queue fallback).
    """
    lru = _LRUCache(lru_max_entries)
    once = _OnceLogger()

    # ── cache ────────────────────────────────────────────────────────────
    async def get_cache(key: str) -> Any:
        if cache_cap is not None:
            try:
                return await cache_cap.get(key)
            except Exception as exc:  # noqa: BLE001 — degrade to LRU
                once.warn_once("cache", "arc.relay.cache_fallback_engaged",
                               error=str(exc),
                               detail="cache.client failing; using in-process LRU "
                                      "(per-worker, not coherent)")
        return lru.get(key)

    async def set_cache(key: str, value: Any, *, ttl: int | None = None) -> None:
        if cache_cap is not None:
            try:
                await cache_cap.set(key, value, ttl=ttl)
                return
            except Exception as exc:  # noqa: BLE001
                once.warn_once("cache", "arc.relay.cache_fallback_engaged",
                               error=str(exc),
                               detail="cache.client failing; using in-process LRU")
        lru.set(key, value, ttl)

    async def rm_cache(key: str) -> None:
        if cache_cap is not None:
            try:
                await cache_cap.delete(key)
                return
            except Exception:  # noqa: BLE001
                pass
        lru.delete(key)

    async def rm_cache_prefix(prefix: str) -> int:
        if cache_cap is not None:
            try:
                return await cache_cap.delete_prefix(prefix)
            except Exception:  # noqa: BLE001
                pass
        return lru.delete_prefix(prefix)

    async def rate_limit(key: str, *, limit: int, period: int) -> bool:
        """Distributed when redix is present; in fallback, allow (the per-process
        limiter in relay's request path remains the backstop)."""
        if cache_cap is not None:
            try:
                return await cache_cap.rate_limit(key, limit=limit, period=period)
            except Exception:  # noqa: BLE001
                pass
        return True

    # ── queue ────────────────────────────────────────────────────────────
    async def enqueue(task: str, **kwargs: Any) -> str | None:
        if queue_cap is not None:
            try:
                return await queue_cap.enqueue(task, **kwargs)
            except Exception as exc:  # noqa: BLE001 — degrade to inline
                once.warn_once("queue", "arc.relay.queue_fallback_engaged",
                               task=task, error=str(exc),
                               detail="queue.client failing; running inline via "
                                      "BackgroundTask (no retry/persistence)")
        else:
            once.warn_once("queue", "arc.relay.queue_fallback_engaged", task=task,
                           detail="queue.client absent (redix not installed); "
                                  "running inline via BackgroundTask "
                                  "(no retry/dead-letter/persistence)")
        # Fallback: run the handler inline, right now. We do not block the
        # caller's flow on the result; failures are logged.
        handler = registrar.task_handler(task)
        if handler is None:
            log.error("arc.relay.task_unknown", task=task)
            return None
        await _run_inline(handler, kwargs)
        return None  # no durable job id in fallback mode

    async def queue_result(job_id: str | None) -> dict | None:
        if queue_cap is not None and job_id is not None:
            try:
                return await queue_cap.result(job_id)
            except Exception:  # noqa: BLE001
                return None
        # Fallback mode never produced a durable job id / result record.
        return None

    # ── scheduler ─────────────────────────────────────────────────────────
    async def schedule_cron(name: str, expr: str, **opts: Any) -> None:
        if scheduler_cap is not None:
            try:
                await scheduler_cap.register_cron(name, expr, **opts)
                return
            except Exception as exc:  # noqa: BLE001
                once.warn_once("sched", "arc.relay.scheduler_fallback",
                               error=str(exc))
        else:
            once.warn_once(
                "sched", "arc.relay.scheduler_fallback",
                detail="scheduler.client unavailable; registered schedules will "
                       "NOT run automatically. Run 'arc schedule export' to "
                       "generate system cron entries.",
            )
        # Registration is bookkeeping only; with no scheduler.client there is
        # nowhere to persist it for the worker, so this is a no-op beyond the
        # one-time warning. The @relay.scheduled handler still exists for
        # `arc schedule run <name>` (system-cron fallback).

    async def schedule_every(name: str, *, seconds: int | None = None,
                             minutes: int | None = None,
                             hours: int | None = None, **opts: Any) -> None:
        total = (seconds or 0) + (minutes or 0) * 60 + (hours or 0) * 3600
        if total <= 0:
            raise ValueError("schedule_every needs a positive interval.")
        if scheduler_cap is not None:
            try:
                await scheduler_cap.register_every(name, seconds=total, **opts)
                return
            except Exception as exc:  # noqa: BLE001
                once.warn_once("sched", "arc.relay.scheduler_fallback",
                               error=str(exc))
        else:
            once.warn_once(
                "sched", "arc.relay.scheduler_fallback",
                detail="scheduler.client unavailable; registered schedules will "
                       "NOT run automatically. Run 'arc schedule export'.",
            )

    async def schedule_history(name: str, *, limit: int = 20) -> list:
        if scheduler_cap is not None:
            try:
                return await scheduler_cap.history(name, limit=limit)
            except Exception:  # noqa: BLE001
                return []
        return []

    return {
        "get_cache": get_cache,
        "set_cache": set_cache,
        "rm_cache": rm_cache,
        "rm_cache_prefix": rm_cache_prefix,
        "rate_limit": rate_limit,
        "enqueue": enqueue,
        "queue_result": queue_result,
        "schedule_cron": schedule_cron,
        "schedule_every": schedule_every,
        "schedule_history": schedule_history,
    }


async def _run_inline(handler, kwargs: dict) -> None:
    """Run a task handler inline (queue fallback). Failures are logged, never
    raised into the caller's request flow."""
    import asyncio
    try:
        result = handler(**kwargs)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001
        log.error("arc.relay.inline_task_failed", error=str(exc))