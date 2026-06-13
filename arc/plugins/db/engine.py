"""
arc.plugins.db.engine
====================
Process-wide async engines, keyed by name. Created by
``DatabasePlugin.startup()`` and exposed as the ``db.engine`` capability.
``standalone_connection()`` lets CLI commands (migrate, backup) open a
connection without booting the whole orchestrator.

Why keyed: the documented multi-DB pattern ("db_hr provides db.hr, db_finance
provides db.finance") previously broke silently — the module-global
``if _engine is None`` guard meant the second DatabasePlugin instance received
the FIRST plugin's engine. Engines are now registered under the owning
plugin's name. Single-DB projects are unaffected: when exactly one engine is
registered, ``get_engine()`` with no key resolves to it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from arc.kernel.exceptions import ArcError
from arc.kernel.logger import get_logger
from arc.plugins.db.config import DatabaseConfig

log = get_logger(__name__)

DEFAULT_KEY = "db"

_engines: dict[str, AsyncEngine] = {}


def create_engine(cfg: DatabaseConfig, key: str = DEFAULT_KEY) -> AsyncEngine:
    """Create (or return) the engine registered under *key*."""
    if key not in _engines:
        if not cfg.url:
            raise ArcError("DATABASE_URL is not configured.", code="arc.db.no_url")
        _engines[key] = create_async_engine(
            cfg.url,
            echo=cfg.echo,
            pool_size=cfg.pool_size,
            max_overflow=cfg.max_overflow,
            pool_timeout=cfg.pool_timeout,
            pool_recycle=cfg.pool_recycle,
            # Force UTC on every connection regardless of the Postgres server
            # timezone setting. Arc always stores timestamps in UTC; the
            # [locale] timezone in arc.toml is for display only.
            connect_args={"server_settings": {"timezone": "UTC"}},
        )
        log.info("arc.db.engine_created", key=key)
    return _engines[key]


def get_engine(key: str | None = None) -> AsyncEngine:
    """Return a registered engine.

    ``key=None`` resolves unambiguously when exactly one engine exists —
    the common single-database case — so existing call sites keep working.
    """
    if key is None:
        if len(_engines) == 1:
            return next(iter(_engines.values()))
        if not _engines:
            raise ArcError(
                "Engine not initialised. DatabasePlugin.startup() must run first.",
                code="arc.db.engine_not_init",
            )
        raise ArcError(
            f"Multiple engines registered ({sorted(_engines)}) — pass a key.",
            code="arc.db.engine_ambiguous",
        )
    engine = _engines.get(key)
    if engine is None:
        raise ArcError(
            f"Engine '{key}' not initialised. DatabasePlugin.startup() must run first.",
            code="arc.db.engine_not_init",
        )
    return engine


async def dispose_engine(key: str | None = None) -> None:
    """Dispose one engine (by key) or all engines (key=None)."""
    if key is None:
        for k in list(_engines):
            await _engines.pop(k).dispose()
        return
    engine = _engines.pop(key, None)
    if engine is not None:
        await engine.dispose()


def reset_engine() -> None:
    """Test helper — forget every registered engine without disposal."""
    _engines.clear()


@asynccontextmanager
async def standalone_connection(
    cfg: DatabaseConfig, *, autocommit: bool = True
) -> AsyncIterator:
    """Open a one-off connection for DDL/migrations/backup (no app boot).

    AUTOCOMMIT by default (the migrator's per-op atomicity is handled by the
    migrator itself via explicit driver transactions). Pass
    ``autocommit=False`` for callers that want SQLAlchemy-managed
    transactions instead.
    """
    kwargs = {"isolation_level": "AUTOCOMMIT"} if autocommit else {}
    engine = create_async_engine(cfg.url, **kwargs)
    try:
        async with engine.connect() as conn:
            yield conn
            if not autocommit:
                await conn.commit()
    finally:
        await engine.dispose()