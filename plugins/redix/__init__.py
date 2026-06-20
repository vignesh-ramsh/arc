"""
plugins.redix
=============
Redis-backed cache, queue, and scheduler — three capabilities from one shared
connection pool. redix imports no other plugin and reaches Redis directly (the
way psqldb reaches Postgres). It is a **soft, optional** dependency of relay:
relay consumes its capabilities via ``rt.capabilities.get(...)`` and falls back
gracefully when redix is absent.

Capabilities provided:
    cache.client       get/set/delete/delete_prefix/rate_limit
    queue.client       enqueue/result + dead-letter (worker consumes)
    scheduler.client   register_cron/register_every/list/history (worker ticks)

No decorators live here — task/schedule *registration* is done through relay's
``@relay.task`` / ``@relay.scheduled`` and the ``arc.*`` facade. These singletons
are bound by ``RedixPlugin.startup()`` once the connection pool is open.
"""

from __future__ import annotations

# Bound at startup() — None until the plugin's connection pool is live.
connection = None       # RedisConnection
cache = None            # CacheClient   (cache.client)
queue = None            # QueueClient   (queue.client)
scheduler = None        # SchedulerClient (scheduler.client)


def _bind(conn, cache_client, queue_client, scheduler_client) -> None:
    global connection, cache, queue, scheduler
    connection = conn
    cache = cache_client
    queue = queue_client
    scheduler = scheduler_client


__all__ = ["connection", "cache", "queue", "scheduler", "_bind"]