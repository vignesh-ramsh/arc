"""
arc.plugins.db.engine
====================
Process-wide async engine. Created by ``DatabasePlugin.startup()`` and exposed
as the ``db.engine`` capability. ``standalone_connection()`` lets CLI commands
(migrate, backup) open a connection without booting the whole orchestrator.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from arc.kernel.exceptions import ArcError
from arc.kernel.logger import get_logger
from arc.plugins.db.config import DatabaseConfig

log = get_logger(__name__)

_engine: AsyncEngine | None = None


def create_engine(cfg: DatabaseConfig) -> AsyncEngine:
    global _engine
    if _engine is None:
        if not cfg.url:
            raise ArcError("DATABASE_URL is not configured.", code="arc.db.no_url")
        _engine = create_async_engine(
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
        log.info("arc.db.engine_created")
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise ArcError(
            "Engine not initialised. DatabasePlugin.startup() must run first.",
            code="arc.db.engine_not_init",
        )
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


def reset_engine() -> None:
    """Test helper."""
    global _engine
    _engine = None


@asynccontextmanager
async def standalone_connection(cfg: DatabaseConfig) -> AsyncIterator:
    """Open a one-off AUTOCOMMIT connection for DDL/migrations (no app boot)."""
    engine = create_async_engine(cfg.url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()