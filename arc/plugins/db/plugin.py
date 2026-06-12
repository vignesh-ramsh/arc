"""
arc.plugins.db.plugin
====================
``DatabasePlugin`` — the bundled database layer, declared in arc.lock like any
other plugin. It provides two capabilities (``db.engine``, ``db.session``) and
contributes schema sources + the ``arc db`` CLI group. Nothing privileges it;
it simply ends up first in the resolved order because everything that touches
the database ``requires "db.session"``.
"""

from __future__ import annotations

from pathlib import Path

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime
from arc.plugins.db.config import DatabaseConfig
from arc.plugins.db.engine import create_engine, dispose_engine, get_engine
from arc.plugins.db.migrations.migrator import SchemaSource
from arc.plugins.db.session import get_session, init_session_factory, reset_session_factory

log = get_logger(__name__)


class DatabasePlugin(Plugin):
    provides = ("db.engine", "db.session")
    requires = ()
    load_order = 0
    critical = True
    description = "Async PostgreSQL (SQLAlchemy 2.0 / asyncpg)"

    def __init__(self) -> None:
        self._cfg: DatabaseConfig | None = None

    @property
    def name(self) -> str:
        return "db"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── setup: resolve config + register capabilities ───────────────────
    def setup(self, rt: Runtime) -> None:
        self._cfg = DatabaseConfig.from_mapping(rt.plugin_config)
        rt.capabilities.provide("db.engine", factory=get_engine, source=self.name)
        rt.capabilities.provide("db.session", instance=get_session, source=self.name)
        rt.capabilities.provide("db.config", instance=self._cfg, source=self.name)

    # ── contribute: schema sources (auto-discovered) + cli ──────────────
    def contribute(self, rt: Runtime) -> None:
        for src in self._discover_schema_sources():
            rt.extensions.contribute(Points.DB_SCHEMA_SOURCES, src, source=self.name)

        from arc.plugins.db.cli import build_cli

        rt.extensions.contribute(Points.CLI_COMMANDS, build_cli(), source=self.name)

    def _discover_schema_sources(self) -> list[SchemaSource]:
        """Find plugins with a schemas/ or patches/ dir at the project root."""
        from arc.kernel.loader import LockFile, find_lock_file
        import json

        sources: list[SchemaSource] = []
        try:
            lock_path = find_lock_file()
        except Exception:
            return sources
        root = lock_path.parent
        try:
            lock = LockFile.model_validate(json.loads(lock_path.read_text("utf-8")))
        except Exception:
            return sources
        for entry in lock.plugins:
            plugin_dir = root / entry.name
            if (plugin_dir / "schemas").is_dir() or (plugin_dir / "patches").is_dir():
                sources.append(SchemaSource(plugin=entry.name, plugin_dir=plugin_dir))
        return sources

    # ── lifecycle ───────────────────────────────────────────────────────
    async def startup(self) -> None:
        assert self._cfg is not None
        engine = create_engine(self._cfg)
        init_session_factory(engine)

    async def shutdown(self) -> None:
        reset_session_factory()
        await dispose_engine()

    # ── checks ──────────────────────────────────────────────────────────
    async def startup_check(self) -> CheckResult:
        cfg = self._cfg
        if cfg is None or not cfg.url:
            return CheckResult.fail(
                "DATABASE_URL is not set. Set it in arc.toml [plugins.db] url= "
                "or as the DATABASE_URL environment variable."
            )
        if "asyncpg" not in cfg.url:
            return CheckResult.fail("DATABASE_URL must use the postgresql+asyncpg:// driver.")
        return CheckResult.ok()

    async def health_check(self) -> CheckResult:
        try:
            from sqlalchemy import text

            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            return CheckResult.ok("database reachable")
        except Exception as exc:
            return CheckResult.fail(f"database unreachable: {exc}")