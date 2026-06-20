"""
plugins.redix.connection
=========================
The one Redis connection pool shared by all three redix capabilities
(cache.client, queue.client, scheduler.client). redix talks to Redis directly,
the same way psqldb talks to Postgres directly — no other plugin reaches Redis.

The pool is created in the plugin's ``startup()`` (inside the event loop) and
disposed in ``shutdown()``. ``ping()`` backs the plugin health check.
"""

from __future__ import annotations

from typing import Any

from arc.kernel.logger import get_logger

log = get_logger("arc.plugin.redix.connection")


class RedisConnection:
    """Owns a single ``redis.asyncio`` connection pool for the process."""

    def __init__(self, url: str, *, max_connections: int = 20) -> None:
        self._url = url
        self._max_connections = max_connections
        self._client: Any = None

    @property
    def url(self) -> str:
        return self._url

    async def connect(self) -> None:
        """Create the pool. Lazy-imports redis so a deployment without redix
        installed never imports it."""
        if self._client is not None:
            return
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "The 'redis' package is required by the redix plugin. "
                "Install it with: pip install 'redis>=5.0'"
            ) from exc

        self._client = Redis.from_url(
            self._url,
            max_connections=self._max_connections,
            decode_responses=False,   # we handle bytes <-> str in serializers
        )
        log.info("arc.redix.connected", url=_safe_url(self._url),
                 max_connections=self._max_connections)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.aclose()
        except Exception as exc:  # pragma: no cover - best-effort teardown
            log.warning("arc.redix.disconnect_error", error=str(exc))
        finally:
            self._client = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError(
                "redix Redis client is not connected — plugin startup() has not "
                "run yet (or connect() failed)."
            )
        return self._client

    async def ping(self) -> bool:
        """True if Redis answers PING. Never raises — returns False on any error
        so callers (health checks, degradation probes) can treat it as a simple
        boolean."""
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception:
            return False


def _safe_url(url: str) -> str:
    """Redact a password in a redis URL for logging."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    creds, _, host = rest.partition("@")
    if ":" in creds:
        user, _, _pw = creds.partition(":")
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"