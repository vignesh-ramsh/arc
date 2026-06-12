"""
arc.plugins.db.cli
=================
The ``arc db`` command group, contributed to ``cli.commands``.

Migrations
    arc db plan [--confirm-destructive]
    arc db migrate [--confirm-destructive]
    arc db status
    arc db connect

Backup / restore
    arc db backup  [-p PLUGIN ...] [--format json|sql] [--key KEY]
    arc db restore [PATH] [-p PLUGIN ...] [--format json|sql] [--key KEY]
    arc db recover -t row|column --id TRASH_ID
    arc db cleanup [-p PLUGIN ...] [--before ISO_DATE] [--dry-run]
    arc db backup-convert PATH [--key KEY] [--out PATH]
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import subprocess
import sys
from pathlib import Path

import typer

from arc.kernel.logger import get_logger
from arc.kernel.registry import Points
from arc.plugins.db import backup as bk
from arc.plugins.db.config import DatabaseConfig
from arc.plugins.db.engine import standalone_connection
from arc.plugins.db.migrations.migrator import (
    SchemaSource,
    build_plan,
    execute,
    read_db_state,
)

log = get_logger(__name__)


def build_cli() -> typer.Typer:
    db_app = typer.Typer(name="db", help="Database migrations and maintenance.")

    # ── plan ─────────────────────────────────────────────────────────────
    @db_app.command("plan")
    def plan_cmd(
        confirm_destructive: bool = typer.Option(False, "--confirm-destructive"),
    ) -> None:
        """Preview DDL that `arc db migrate` would apply (no data touched)."""
        sources = _collect_sources()
        cfg = _require_url()

        async def _run():
            async with standalone_connection(cfg) as conn:
                return await read_db_state(conn)

        state = asyncio.run(_run())
        plan = build_plan(sources, state, confirm_destructive=confirm_destructive)
        _print_issues(plan)
        if plan.is_empty():
            typer.echo("Nothing to migrate.")
            return
        typer.echo(
            f"\n-- {len(plan.ops)} operation(s), {len(plan.tables_created)} new "
            f"table(s), {plan.destructive_count} destructive\n"
        )
        for stmt in plan.all_statements():
            typer.echo(stmt)

    # ── migrate ──────────────────────────────────────────────────────────
    @db_app.command("migrate")
    def migrate_cmd(
        confirm_destructive: bool = typer.Option(
            False, "--confirm-destructive",
            help="Allow DROP COLUMN / DROP+ADD (data moved to _trash first).",
        ),
    ) -> None:
        """Create/update tables for every plugin's schemas and patches."""
        sources = _collect_sources()
        cfg = _require_url()

        async def _run():
            # Two separate AUTOCOMMIT connections: one read, one write.
            async with standalone_connection(cfg) as read_conn:
                state = await read_db_state(read_conn)
            plan = build_plan(sources, state, confirm_destructive=confirm_destructive)
            _print_issues(plan)
            if plan.has_errors:
                return None
            if plan.is_empty():
                return {"executed": 0, "ops": 0}
            async with standalone_connection(cfg) as exec_conn:
                return await execute(plan, exec_conn)

        result = asyncio.run(_run())
        if result is None:
            typer.echo("\n✗ Migration blocked by errors above. Nothing applied.")
            raise typer.Exit(1)
        if result["ops"] == 0:
            typer.echo("Nothing to migrate.")
            return
        typer.echo(f"✓ Applied {result['executed']} statement(s) across {result['ops']} op(s).")

    # ── status ───────────────────────────────────────────────────────────
    @db_app.command("status")
    def status_cmd() -> None:
        """List Arc-managed tables and the live field registry."""
        cfg = _require_url()

        async def _run():
            async with standalone_connection(cfg) as conn:
                return await read_db_state(conn)

        state = asyncio.run(_run())
        user_tables = sorted(t for t in state.existing_tables if not t.startswith("_"))
        if not user_tables:
            typer.echo("No Arc-managed tables yet. Run `arc db migrate`.")
            return
        typer.echo(f"  {'TABLE':<24} FIELDS (fld_id → name : type)")
        typer.echo(f"  {'-'*24} {'-'*40}")
        for table in user_tables:
            fields = state.registry.get(table, {})
            typer.echo(f"  {table}")
            for fld_id, e in sorted(fields.items()):
                req = " *" if e.reqd else ""
                typer.echo(f"      {fld_id} → {e.field_name} : {e.type}{req}")

    # ── connect ───────────────────────────────────────────────────────────
    @db_app.command("connect")
    def connect_cmd() -> None:
        """Open an interactive psql shell connected to the project database."""
        psql = _find_psql()
        if psql is None:
            typer.echo(
                "psql not found. Install it:\n"
                "  Ubuntu/Debian : sudo apt install postgresql-client\n"
                "  macOS         : brew install libpq && brew link --force libpq"
            )
            raise typer.Exit(1)

        cfg = _require_url()
        dsn = cfg.url.replace("postgresql+asyncpg://", "postgresql://")
        _print_connect_banner(dsn)

        env = {**os.environ, "PGPASSWORD": _extract_password(dsn)}
        if sys.platform == "win32":
            result = subprocess.run([psql, dsn], env=env)
            raise typer.Exit(result.returncode)
        else:
            os.execvpe(psql, [psql, dsn], env)

    # ── backup ───────────────────────────────────────────────────────────
    @db_app.command("backup")
    def backup_cmd(
        plugin: list[str] = typer.Option(None, "-p", "--plugin"),
        format: str = typer.Option("json", "--format", help="json (default) | sql"),
        key: str = typer.Option(None, "--key", help="Encryption passphrase."),
    ) -> None:
        """Back up Arc-managed tables to backups/ (JSON by default)."""
        fmt = _check_format(format)
        cfg = _require_url()
        settings = _backup_settings()
        passphrase = key or _resolve_key(settings)
        encrypt = bool(key) or bool(settings.get("encrypt"))
        if encrypt and not passphrase:
            typer.echo("Encryption requested but no key found (--key or arc.toml key env).")
            raise typer.Exit(1)

        async def _run():
            async with standalone_connection(cfg) as conn:
                return await bk.read_backup(conn, list(plugin) or None)

        backup = asyncio.run(_run())
        data = bk.serialize_sql(backup) if fmt == "sql" else bk.serialize_json(backup)
        if encrypt:
            try:
                data = bk.encrypt_bytes(data, passphrase)
            except ImportError:
                typer.echo("pip install cryptography  # required for encryption")
                raise typer.Exit(1)

        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
        scope = ("_" + "-".join(sorted(plugin))) if plugin else ""
        ext = f".{fmt}" + (".enc" if encrypt else "")
        out = _backups_dir() / f"arc_backup{scope}_{ts}{ext}"
        out.write_bytes(data)
        rows = sum(len(t.rows) for t in backup.tables)
        typer.echo(f"✓ {len(backup.tables)} table(s), {rows} row(s) → {out}")
        if encrypt:
            typer.echo("  (encrypted)")

    # ── restore ──────────────────────────────────────────────────────────
    @db_app.command("restore")
    def restore_cmd(
        path: str = typer.Argument(None),
        plugin: list[str] = typer.Option(None, "-p", "--plugin"),
        format: str = typer.Option(None, "--format", help="json | sql"),
        key: str = typer.Option(None, "--key"),
    ) -> None:
        """Restore from a backup. JSON = partial; SQL = strict schema match."""
        cfg = _require_url()
        file = Path(path) if path else _latest_backup(format)
        if file is None or not file.exists():
            typer.echo("No backup file found.")
            raise typer.Exit(1)

        raw = file.read_bytes()
        if bk.is_encrypted(raw):
            passphrase = key or _resolve_key(_backup_settings())
            if not passphrase:
                typer.echo("Backup is encrypted — provide --key.")
                raise typer.Exit(1)
            try:
                raw = bk.decrypt_bytes(raw, passphrase)
            except Exception as exc:
                typer.echo(f"✗ {exc}")
                raise typer.Exit(1)

        fmt = _detect_format(file, raw, format)

        async def _run():
            async with standalone_connection(cfg) as conn:
                if fmt == "sql":
                    return ("sql", await bk.restore_sql(conn, raw))
                b = bk.parse_json(raw)
                if plugin:
                    b.tables = [t for t in b.tables if t.plugin in set(plugin)]
                return ("json", await bk.restore_json(conn, b))

        try:
            kind, result = asyncio.run(_run())
        except Exception as exc:
            typer.echo(f"✗ {exc}")
            raise typer.Exit(1)

        if kind == "sql":
            typer.echo(f"✓ Restored (strict) — {result['executed']} statement(s).")
        else:
            typer.echo(
                f"✓ Restored (partial) — {result['inserted']} row(s); "
                f"{result['skipped_tables']} table(s) skipped."
            )

    # ── recover ──────────────────────────────────────────────────────────
    @db_app.command("recover")
    def recover_cmd(
        type: str = typer.Option(..., "-t", "--type", help="row | column"),
        id: int = typer.Option(..., "--id", help="_trash row id"),
    ) -> None:
        """Recover a deleted row or dropped column from _trash."""
        if type not in ("row", "column"):
            typer.echo("--type must be 'row' or 'column'.")
            raise typer.Exit(1)
        cfg = _require_url()

        async def _run():
            async with standalone_connection(cfg) as conn:
                if type == "row":
                    return await bk.recover_row(conn, id)
                return await bk.recover_column(conn, id)

        try:
            target = asyncio.run(_run())
        except Exception as exc:
            typer.echo(f"✗ {exc}")
            raise typer.Exit(1)
        typer.echo(f"✓ Recovered {type}: {target}")

    # ── cleanup ──────────────────────────────────────────────────────────
    @db_app.command("cleanup")
    def cleanup_cmd(
        plugin: list[str] = typer.Option(None, "-p", "--plugin"),
        before: str = typer.Option(None, "--before", help="ISO date cutoff."),
        dry_run: bool = typer.Option(False, "--dry-run"),
    ) -> None:
        """Permanently purge _trash entries older than the cutoff date."""
        cfg = _require_url()
        settings = _backup_settings()
        if before:
            cutoff = _dt.datetime.fromisoformat(before)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=_dt.timezone.utc)
        else:
            days = int(settings.get("retention_days", 30))
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)

        async def _run():
            async with standalone_connection(cfg) as conn:
                return await bk.cleanup_trash(conn, cutoff, list(plugin) or None, dry_run)

        n = asyncio.run(_run())
        verb = "Would purge" if dry_run else "✓ Purged"
        typer.echo(f"{verb} {n} _trash row(s) older than {cutoff.date().isoformat()}.")

    # ── backup-convert ────────────────────────────────────────────────────
    @db_app.command("backup-convert")
    def convert_cmd(
        path: str = typer.Argument(...),
        key: str = typer.Option(None, "--key"),
        out: str = typer.Option(None, "--out"),
    ) -> None:
        """Convert an Arc SQL backup to JSON format (enables partial restore)."""
        src = Path(path)
        if not src.exists():
            typer.echo(f"File not found: {src}")
            raise typer.Exit(1)
        raw = src.read_bytes()
        if bk.is_encrypted(raw):
            passphrase = key or _resolve_key(_backup_settings())
            if not passphrase:
                typer.echo("File is encrypted — provide --key.")
                raise typer.Exit(1)
            raw = bk.decrypt_bytes(raw, passphrase)
        try:
            json_bytes = bk.sql_to_json(raw)
        except Exception as exc:
            typer.echo(f"✗ {exc}")
            raise typer.Exit(1)
        dest = Path(out) if out else src.with_suffix(".json")
        dest.write_bytes(json_bytes)
        typer.echo(f"✓ Converted → {dest}")

    return db_app


# ── helpers ──────────────────────────────────────────────────────────────────
def _collect_sources() -> list[SchemaSource]:
    from arc.kernel.orchestrator import Arc

    arc = Arc()
    arc.build()
    return list(arc.extensions.get(Points.DB_SCHEMA_SOURCES))


def _project_root() -> Path:
    from arc.kernel.loader import find_lock_file

    return find_lock_file().parent


def _backups_dir() -> Path:
    d = _project_root() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_config() -> DatabaseConfig:
    from arc.kernel.config import load_config

    try:
        cfg = load_config(_project_root() / "arc.toml")
        return DatabaseConfig.from_mapping(cfg.for_plugin("db"))
    except Exception:
        return DatabaseConfig.from_mapping({})


def _backup_settings() -> dict:
    from arc.kernel.config import load_config

    try:
        cfg = load_config(_project_root() / "arc.toml")
        return dict(cfg.for_plugin("db").get("backup", {}))
    except Exception:
        return {}


def _resolve_key(settings: dict) -> str | None:
    env_name = settings.get("encryption_key_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return settings.get("encryption_key") or None


def _require_url() -> DatabaseConfig:
    cfg = _db_config()
    if not cfg.url:
        typer.echo("DATABASE_URL is not configured.")
        raise typer.Exit(1)
    return cfg


def _check_format(fmt: str) -> str:
    fmt = fmt.lower()
    if fmt not in ("json", "sql"):
        typer.echo("--format must be 'json' or 'sql'.")
        raise typer.Exit(1)
    return fmt


def _detect_format(file: Path, raw: bytes, override: str | None) -> str:
    if override:
        return _check_format(override)
    name = file.name.replace(".enc", "")
    if name.endswith(".sql"):
        return "sql"
    if name.endswith(".json"):
        return "json"
    return "json" if raw.lstrip()[:1] == b"{" else "sql"


def _latest_backup(fmt: str | None) -> Path | None:
    d = _project_root() / "backups"
    if not d.is_dir():
        return None
    files = [p for p in d.iterdir() if p.is_file() and p.name.startswith("arc_backup")]
    if fmt:
        files = [p for p in files if f".{fmt}" in p.name]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _find_psql() -> str | None:
    import shutil

    return shutil.which("psql")


def _extract_password(dsn: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(dsn).password or ""
    except Exception:
        return ""


def _print_connect_banner(dsn: str) -> None:
    from urllib.parse import urlparse

    p = urlparse(dsn)
    typer.echo(f"\n  Connecting to PostgreSQL")
    typer.echo(f"  Host : {p.hostname}:{p.port or 5432}")
    typer.echo(f"  DB   : {(p.path or '/').lstrip('/') or 'postgres'}")
    typer.echo(f"  User : {p.username or ''}")
    typer.echo(f"\n  \\q to exit  \\dt to list tables  \\? for help\n")


def _print_issues(plan) -> None:
    if not plan.lint_issues:
        return
    typer.echo("Lint:")
    for issue in plan.lint_issues:
        typer.echo(f"  {issue}")