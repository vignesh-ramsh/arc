"""
plugins.redix.cache.client
===========================
The ``cache.client`` capability implementation. Plain async methods (no
decorators) over the shared Redis connection. relay is the only consumer — it
calls these from ``arc.get_cache`` / ``arc.set_cache`` etc. and falls back to an
in-process LRU when this capability is absent or a call raises.

Values are JSON-serialized (see redix.serializers). ``rate_limit`` implements a
fixed-window counter shared across every worker/process — the documented limit
becomes the actual limit, unlike relay's per-process in-memory fallback.
"""

from __future__ import annotations

from typing import Any

from arc.kernel.logger import get_logger
from plugins.redix.connection import RedisConnection
from plugins.redix.keys import KeyBuilder
from plugins.redix.serializers import decode, encode

log = get_logger("arc.plugin.redix.cache")


class CacheClient:
    def __init__(self, conn: RedisConnection, *, key_prefix: str = "arc:cache",
                 default_ttl: int = 300) -> None:
        self._conn = conn
        self._keys = KeyBuilder(key_prefix)
        self._default_ttl = default_ttl

    # ── basic KV ────────────────────────────────────────────────────────
    async def get(self, key: str) -> Any:
        raw = await self._conn.client.get(self._keys.build(key))
        return decode(raw)

    async def set(self, key: str, value: Any, *, ttl: int | None = None) -> None:
        ttl = self._default_ttl if ttl is None else ttl
        payload = encode(value)
        rkey = self._keys.build(key)
        if ttl and ttl > 0:
            await self._conn.client.set(rkey, payload, ex=ttl)
        else:
            await self._conn.client.set(rkey, payload)

    async def delete(self, key: str) -> None:
        await self._conn.client.delete(self._keys.build(key))

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key under ``{key_prefix}:{prefix}*``. Uses SCAN (never
        KEYS) so it is safe on large keyspaces. Returns the count removed."""
        client = self._conn.client
        match = self._keys.build(prefix) + "*"
        removed = 0
        batch: list[bytes] = []
        async for rkey in client.scan_iter(match=match, count=500):
            batch.append(rkey)
            if len(batch) >= 500:
                removed += await client.delete(*batch)
                batch.clear()
        if batch:
            removed += await client.delete(*batch)
        return removed

    async def exists(self, key: str) -> bool:
        return bool(await self._conn.client.exists(self._keys.build(key)))

    # ── distributed rate limiting (fixed window) ────────────────────────
    async def rate_limit(self, key: str, *, limit: int, period: int) -> bool:
        """Fixed-window counter. Returns True if the call is ALLOWED (under the
        limit), False if it should be rejected.

        Increments a per-window counter and sets its expiry on first hit. The
        window key embeds the period so a new window starts cleanly. Shared
        across all workers/processes via Redis.
        """
        client = self._conn.client
        rkey = self._keys.build("rl", key)
        # INCR is atomic; set TTL only when the counter is freshly created.
        current = await client.incr(rkey)
        if current == 1:
            await client.expire(rkey, period)
        return current <= limit

    # ── health ──────────────────────────────────────────────────────────
    async def ping(self) -> bool:
        return await self._conn.ping()