"""
arc.plugins.psqldb.plugin
====================
``DatabasePlugin`` — the bundled database layer, declared in arc.lock like any
other plugin. It provides two capabilities (``db.engine``, ``db.session``) and
contributes schema sources + the ``arc db`` CLI group. Nothing privileges it;
it simply ends up first in the resolved order because everything that touches
the database ``requires "db.session"``.

Multi-DB correctness: the engine and session factory are registered under
THIS instance's plugin name (see engine.py / session.py). Two DatabasePlugin
instances in one process (the documented db_hr / db_finance pattern) now own
two independent engines — previously the module-global guard handed the
second instance the first instance's engine.
"""

from __future__ import annotations

from pathlib import Path

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime
from arc.plugins.psqldb.config import DatabaseConfig
from arc.plugins.psqldb.engine import create_engine, dispose_engine, get_engine
from arc.plugins.psqldb.migrations.migrator import SchemaSource
from arc.plugins.psqldb.session import init_session_factory, make_session_cm, reset_session_factory

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
        return "psqldb"

    @property
    def version(self) -> str:
        return "1.0.0"

    # ── setup: resolve config + register capabilities ───────────────────
    def setup(self, rt: Runtime) -> None:
        self._cfg = DatabaseConfig.from_mapping(rt.plugin_config)
        key = self.name
        # The engine factory and session cm resolve lazily by key, so they
        # can be provided here (sync pass) even though the engine itself is
        # created in startup() inside the event loop.
        rt.capabilities.provide(
            "db.engine", factory=lambda: get_engine(key), source=self.name
        )
        rt.capabilities.provide(
            "db.session", instance=make_session_cm(key), source=self.name
        )
        rt.capabilities.provide("db.config", instance=self._cfg, source=self.name)

    # ── contribute: schema sources (auto-discovered) + cli ──────────────
    def contribute(self, rt: Runtime) -> None:
        for src in self._discover_schema_sources():
            rt.extensions.contribute(Points.DB_SCHEMA_SOURCES, src, source=self.name)

        from arc.plugins.psqldb.cli import build_cli

        rt.extensions.contribute(Points.CLI_COMMANDS, build_cli(), source=self.name)

    def _discover_schema_sources(self) -> list[SchemaSource]:
        """Find plugins with a schemas/ or patches/ dir at the project root.

        Discovery order no longer affects correctness — the migrator plans all
        schemas across all sources before any patch (see migrator.build_plan)
        — but entries are still emitted in lock order for stable plan output.
        """
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
        seen: set[str] = set()
        for entry in lock.plugins:
            if entry.name in seen:
                continue
            seen.add(entry.name)
            plugin_dir = root / entry.name
            if (plugin_dir / "schemas").is_dir() or (plugin_dir / "patches").is_dir():
                sources.append(SchemaSource(plugin=entry.name, plugin_dir=plugin_dir))
        return sources

    # ── lifecycle ───────────────────────────────────────────────────────
    async def startup(self) -> None:
        assert self._cfg is not None
        engine = create_engine(self._cfg, key=self.name)
        init_session_factory(engine, key=self.name)

    async def shutdown(self) -> None:
        reset_session_factory(self.name)
        await dispose_engine(self.name)

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

            async with get_engine(self.name).connect() as conn:
                await conn.execute(text("SELECT 1"))
            return CheckResult.ok("database reachable")
        except Exception as exc:
            return CheckResult.fail(f"database unreachable: {exc}")