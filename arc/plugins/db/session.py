"""
arc.plugins.db.session
=====================
The ``db.session`` capability. ``get_session()`` is the canonical way for any
plugin or handler to obtain an ``AsyncSession``:

    async with get_session() as session:
        await session.execute(...)

Commits on clean exit, rolls back on exception, always closes. Autoflush and
expire-on-commit are off for async safety.

Factories are keyed by the owning DatabasePlugin's name (mirroring
``engine.py``) so the documented multi-DB pattern works: each db plugin
instance owns its own factory instead of all instances silently sharing one
module global. Single-DB projects are unaffected — with exactly one factory
registered, ``get_session()`` resolves to it with no key.

``read_only=True`` skips the commit on clean exit — pure SELECT paths (the
API list/get handlers) no longer pay a COMMIT round-trip per request.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from arc.kernel.exceptions import ArcError
from arc.kernel.logger import get_logger

log = get_logger(__name__)

DEFAULT_KEY = "db"

_factories: dict[str, async_sessionmaker[AsyncSession]] = {}


def init_session_factory(engine, key: str = DEFAULT_KEY) -> async_sessionmaker[AsyncSession]:
    if key not in _factories:
        _factories[key] = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        log.debug("arc.db.session_factory_created", key=key)
    return _factories[key]


def get_session_factory(key: str | None = None) -> async_sessionmaker[AsyncSession]:
    if key is None:
        if len(_factories) == 1:
            return next(iter(_factories.values()))
        if not _factories:
            raise ArcError(
                "Session factory not initialised — has DatabasePlugin started?",
                code="arc.db.session_not_init",
            )
        raise ArcError(
            f"Multiple session factories registered ({sorted(_factories)}) — pass a key.",
            code="arc.db.session_ambiguous",
        )
    factory = _factories.get(key)
    if factory is None:
        raise ArcError(
            f"Session factory '{key}' not initialised — has DatabasePlugin started?",
            code="arc.db.session_not_init",
        )
    return factory


def reset_session_factory(key: str | None = None) -> None:
    if key is None:
        _factories.clear()
    else:
        _factories.pop(key, None)


@asynccontextmanager
async def get_session(read_only: bool = False) -> AsyncIterator[AsyncSession]:
    """Session bound to the sole registered factory (single-DB projects)."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            if not read_only:
                await session.commit()
        except Exception:
            await session.rollback()
            raise


def make_session_cm(key: str) -> Callable:
    """Build a ``db.session``-shaped context-manager callable bound to *key*.

    This is what each DatabasePlugin instance registers as its capability, so
    two db plugins provide two independent session sources. The factory is
    resolved lazily at call time — the capability can be provided in the
    synchronous setup pass even though the engine is created in startup().
    """

    @asynccontextmanager
    async def session_cm(read_only: bool = False) -> AsyncIterator[AsyncSession]:
        factory = get_session_factory(key)
        async with factory() as session:
            try:
                yield session
                if not read_only:
                    await session.commit()
            except Exception:
                await session.rollback()
                raise

    return session_cm