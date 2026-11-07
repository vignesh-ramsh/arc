"""
arc.backupcmd
-----------------
`arc backup create | verify | restore` - the command line over arc.backup (read that module's
docstring for what a backup holds and what a restore refuses to do).

Named backupcmd.py, like healthcmd.py, so it never collides with the arc.backup module it wraps.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Any

import tomlkit
import typer
from rich.console import Console
from rich.markup import escape

from . import backup, registry
from .runtime import find_project_root
from .settings import SettingsError, SettingsManager

console = Console()
err_console = Console(stderr=True, style="bold red")

app = typer.Typer(
    help="Full backups: database, settings store, master key (passphrase-protected), config and files.",
    no_args_is_help=True,
)

PASSPHRASE_ENV = "ARC_BACKUP_PASSPHRASE"
FILER_ROOT_KEY = "filer_local_root"
FILER_STORAGE_KEY = "filer_default_storage"
DSN_KEY = "pgdb_dsn"


def _fail(message: str) -> typer.Exit:
    err_console.print(escape(message))
    return typer.Exit(code=1)


def _passphrase(file: Path | None, *, confirm: bool = False, prompt: str = "Backup passphrase") -> str:
    """From --passphrase-file, else $ARC_BACKUP_PASSPHRASE, else asked for (hidden)."""
    if file is not None:
        try:
            return file.read_text().rstrip("\r\n")
        except OSError as exc:
            raise _fail(f"cannot read the passphrase file: {exc}")
    if os.environ.get(PASSPHRASE_ENV):
        return os.environ[PASSPHRASE_ENV]
    first = getpass.getpass(f"{prompt}: ")
    if confirm and getpass.getpass("Repeat it: ") != first:
        raise _fail("the two passphrases differ")
    return first


def _project() -> tuple[Path, SettingsManager]:
    root = find_project_root()
    if root is None:
        raise _fail("Not an ARC project (no .arc/arc.toml found here or in any parent).")
    return root, SettingsManager(root / ".arc")


def _pgdump() -> Any | None:
    try:
        from pgdb import dump
    except ImportError:
        return None
    return dump


def _project_name(root: Path) -> str:
    try:
        return str(tomlkit.parse((root / ".arc" / "arc.toml").read_text())["project"]["name"])
    except Exception:  # noqa: BLE001 - a name is for the filename only
        return root.name


def _plugin_versions(root: Path) -> dict[str, str]:
    try:
        doc = registry.load_lock(root / ".arc" / "plugins.lock")
        return {str(name): str(entry.get("version", "")) for name, entry in doc.get("plugins", {}).items()}
    except Exception:  # noqa: BLE001 - informational only
        return {}


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


@app.command("create")
def create_(
    out: Path = typer.Option(None, "--out", help="Where to write it. Default: backups/full/arc-backup-<project>-<time>.tar"),
    passphrase_file: Path = typer.Option(
        None, "--passphrase-file", help=f"Read the passphrase from this file (else ${PASSPHRASE_ENV}, else asked)."
    ),
    keep: int = typer.Option(None, "--keep", min=1, help="Afterwards delete all but the newest N backups in that directory."),
    no_database: bool = typer.Option(False, "--no-database", help="Leave the database out."),
    no_files: bool = typer.Option(False, "--no-files", help="Leave the filer's local files out."),
) -> None:
    """Write one file holding everything needed to bring this instance back: the database, the
    settings store, the master key (encrypted with your passphrase), the config and the uploaded
    files. Never overwrites a backup. Check it with `arc backup verify`; prove it with a restore."""
    root, mgr = _project()
    passphrase = _passphrase(passphrase_file, confirm=True)  # backup.create() refuses a short one, before dumping anything

    dump_database = database_name = None
    pgdump = _pgdump()
    dsn = mgr.get(DSN_KEY, reveal=True) if pgdump is not None else None
    if no_database:
        console.print("[yellow]database left out (--no-database)[/yellow]")
    elif dsn:
        try:
            pgdump.require_tools("pg_dump")
        except pgdump.DumpError as exc:
            raise _fail(str(exc))
        database_name = pgdump.connection_args(dsn)[1]
        dump_database = lambda path: pgdump.dump(dsn, path)  # noqa: E731
    else:
        console.print("[yellow]no database configured (pgdb_dsn is not set): the backup has no database dump[/yellow]")

    file_roots: list[backup.FileRoot] = []
    if not no_files:
        override = mgr.get(FILER_ROOT_KEY)
        files_dir = Path(override) if override else root / "files"
        if files_dir.is_dir():
            try:
                relative = files_dir.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                relative = None
            file_roots.append(backup.FileRoot("filer", files_dir, relative))
        if mgr.get(FILER_STORAGE_KEY) == "s3":
            console.print("[yellow]files are stored in S3 (filer_default_storage): they are not in this backup - use the bucket's own versioning / replication[/yellow]")

    if out is None:
        out = root / "backups" / "full" / backup.archive_name(_project_name(root))
    try:
        import arc as arc_pkg

        manifest = backup.create(
            root,
            out,
            passphrase=passphrase,
            project_name=_project_name(root),
            arc_version=getattr(arc_pkg, "__version__", "?"),
            plugins=_plugin_versions(root),
            dump_database=dump_database,
            database_name=database_name,
            file_roots=file_roots,
        )
    except (backup.BackupError, SettingsError) as exc:
        raise _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - pg_dump failures etc.: report, do not traceback
        if pgdump is not None and isinstance(exc, pgdump.DumpError):
            raise _fail(str(exc))
        raise

    size = out.stat().st_size
    console.print(f"[bold green]Backup written:[/bold green] {out} ({_human(size)})")
    console.print(
        f"  database: {'yes (' + str(database_name) + ')' if manifest['database']['included'] else 'no'}"
        f" | members: {len(manifest['members'])}"
        + "".join(f" | files {escape('[' + f['label'] + ']')}: {f['files']} ({_human(f['bytes'])})" for f in manifest["files"])
    )
    for warning in manifest["warnings"]:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")
    if backup.ensure_gitignored(root, out.parent):
        console.print(f"[dim]added {out.parent.relative_to(root).as_posix()}/* to .gitignore: a backup holds business data[/dim]")
    console.print(
        "[dim]The passphrase protects only the master key. The database and files inside are not "
        "encrypted - keep this file on encrypted storage or encrypt it at rest.[/dim]"
    )
    if keep is not None:
        for gone in backup.prune(out.parent, keep, protect=out):
            console.print(f"[dim]removed old backup {gone.name}[/dim]")


@app.command("verify")
def verify_(
    archive: Path = typer.Argument(..., exists=True, dir_okay=False, help="A backup written by `arc backup create`."),
    passphrase_file: Path = typer.Option(None, "--passphrase-file", help=f"Else ${PASSPHRASE_ENV}, else asked."),
    no_key_check: bool = typer.Option(False, "--no-key-check", help="Do not ask for the passphrase; skip checking that the key opens."),
) -> None:
    """Check a backup is whole and usable without restoring it: every checksum, that the master key
    opens with your passphrase, that the settings store is intact and that the database dump is a
    readable pg_dump archive. Exits 1 if anything is wrong."""
    passphrase = None if no_key_check else _passphrase(passphrase_file, prompt="Backup passphrase (to check the key opens)")
    pgdump = _pgdump()
    try:
        manifest, problems = backup.verify(
            archive, passphrase, check_dump=(lambda path: pgdump.describe(path)) if pgdump is not None else None
        )
    except backup.BackupError as exc:
        raise _fail(str(exc))
    if problems:
        err_console.print(f"{escape(archive.name)} is NOT sound:")
        for problem in problems:
            err_console.print(f"  - {escape(problem)}")
        raise typer.Exit(code=1)
    console.print(
        f"[bold green]OK[/bold green] {archive.name}: {len(manifest['members'])} members, project "
        f"{manifest['project']!r}, made {manifest['created_at']} by arc {manifest['arc_version']}"
        + ("" if manifest["database"]["included"] else " [yellow](no database dump inside)[/yellow]")
    )
    for warning in manifest["warnings"]:
        console.print(f"[yellow]warning when it was made:[/yellow] {escape(warning)}")
    if passphrase is None:
        console.print("[dim]the master key was not checked (--no-key-check)[/dim]")
    if pgdump is None and manifest["database"]["included"]:
        console.print("[dim]the database dump was not checked: the pgdb plugin is not installed here[/dim]")


@app.command("restore")
def restore_(
    archive: Path = typer.Argument(..., exists=True, dir_okay=False, help="A backup written by `arc backup create`."),
    into: Path = typer.Option(..., "--into", help="The project directory to rebuild. Must not contain .arc/ yet."),
    database_url: str = typer.Option(
        None, "--database-url", help="The database to restore INTO (postgresql://user:pass@host/db), normally an empty new one. Never defaults to the original."
    ),
    skip_database: bool = typer.Option(False, "--skip-database", help="Do not restore the database dump."),
    skip_files: bool = typer.Option(False, "--skip-files", help="Do not restore the uploaded files."),
    passphrase_file: Path = typer.Option(None, "--passphrase-file", help=f"Else ${PASSPHRASE_ENV}, else asked."),
    force: bool = typer.Option(False, "--force", help="Write over an existing .arc/, and restore into a database that already has tables (replacing them)."),
) -> None:
    """Rebuild a project's state from a backup into a directory, and its database into a database
    you name. The code (arc/, plugins/) is not in a backup: reinstall it from your repositories.
    Everything is verified and the key decrypted before anything is written."""
    try:
        manifest = backup.read_manifest(archive)
    except backup.BackupError as exc:
        raise _fail(str(exc))
    wants_database = manifest["database"]["included"] and not skip_database
    pgdump = _pgdump()
    if wants_database:
        if not database_url:
            raise _fail(
                "this backup contains a database. Say where to restore it with --database-url "
                "(an empty database), or pass --skip-database. It is never restored to the original "
                "automatically: that is the live database."
            )
        if pgdump is None:
            raise _fail("restoring the database needs the pgdb plugin (pg_restore) on this machine")
        try:
            pgdump.require_tools("pg_restore")
            if not force and not pgdump.is_empty(database_url):
                raise _fail(
                    "the target database already has tables. Restore into an empty database, or pass "
                    "--force to replace what is there."
                )
        except pgdump.DumpError as exc:
            raise _fail(str(exc))
        except OSError as exc:
            raise _fail(f"cannot reach the target database: {exc}")
    passphrase = _passphrase(passphrase_file, prompt="Backup passphrase")
    console.print(
        f"Restoring the backup of {manifest['project']!r} made {manifest['created_at']} "
        f"(arc {manifest['arc_version']}) into {into}"
    )
    try:
        result = backup.restore(
            archive,
            into,
            passphrase=passphrase,
            restore_database=(lambda path: pgdump.restore(database_url, path, replace=force, portable=True)) if wants_database else None,
            skip_files=skip_files,
            force=force,
        )
    except backup.BackupError as exc:
        raise _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        if pgdump is not None and isinstance(exc, pgdump.DumpError):
            raise _fail(f"{exc}\nThe local state was restored to {into}, the database was not.")
        raise

    restored = SettingsManager(into / ".arc")
    if result.database_restored:
        restored.set(DSN_KEY, database_url, secret=True)  # the restored project must not point at the original
    for label, path in result.files.items():
        # An absolute filer_local_root in the restored store still names the OLD location: point it at
        # where the files now are. (Left unset it means <project>/files, which is where they went.)
        if label == "filer" and restored.get(FILER_ROOT_KEY):
            restored.set(FILER_ROOT_KEY, str(path.resolve()))
    console.print(f"[bold green]Restored[/bold green] into {into}")
    console.print(
        f"  .arc: master key, settings store, config | database: "
        f"{'restored into ' + pgdump.connection_args(database_url)[1] if result.database_restored else 'not restored'}"
        + "".join(f" | files {escape('[' + label + ']')}: {escape(str(path))}" for label, path in result.files.items())
    )
    if manifest["database"]["included"] and not result.database_restored:
        console.print(
            "[yellow]The database was NOT restored, and this project's pgdb_dsn still points at the "
            "original database. Point it at the right one before starting anything: "
            "`arc settings set pgdb_dsn <url> --secret`.[/yellow]"
        )
    console.print(
        "[dim]Next: reinstall the code (arc/ and plugins/ come from your repositories, at the versions in "
        ".arc/plugins.lock), then `arc doctor`, `arc pgdb status` and `arc status`.[/dim]"
    )
