"""
arc.plugins.db.session
=====================
The ``db.session`` capability. ``get_session()`` is the canonical way for any
plugin or handler to obtain an ``AsyncSession``:

    async with get_session() as session:
        await session.execute(...)

Commits on clean exit, rolls back on exception, always closes. Autoflush and
expire-on-commit are off for async safety.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from arc.kernel.exceptions import ArcError
from arc.kernel.logger import get_logger

log = get_logger(__name__)

_factory: async_sessionmaker[AsyncSession] | None = None


def init_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        log.debug("arc.db.session_factory_created")
    return _factory


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _factory is None:
        raise ArcError(
            "Session factory not initialised — has DatabasePlugin started?",
            code="arc.db.session_not_init",
        )
    return _factory


def reset_session_factory() -> None:
    global _factory
    _factory = None


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
