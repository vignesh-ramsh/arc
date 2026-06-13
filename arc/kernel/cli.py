"""
arc.kernel.cli
=============
The ``arc`` command-line root.

Static commands: init, new-plugin, run, doctor, clear-cache.
Plugin commands are contributed via the ``cli.commands`` extension point
(arc db, arc api, etc.) and mounted at startup.

One build per process: every command that needs a built app goes through
``Arc.shared()``, so the CLI no longer imports every plugin and re-configures
logging two (or more) times per invocation. If the build fails inside a
project, the error is printed visibly instead of being swallowed at debug
level — a broken plugin previously just made ``arc db`` silently disappear
from the command list.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from arc.kernel.loader import LockEntry, LockFile, PluginLoader, find_lock_file
from arc.kernel.logger import get_logger

log = get_logger(__name__)
app = typer.Typer(name="arc", help="Arc — everything is a plugin.", no_args_is_help=True)


def _echo(msg: str) -> None:
    typer.echo(msg)


# ── init ────────────────────────────────────────────────────────────────
@app.command()
def init(
    directory: str = typer.Argument(".", help="Project directory"),
    name: str = typer.Option("arc-app", help="App name"),
) -> None:
    """Create arc.toml + arc.lock with the bundled db, api and http plugins."""
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)

    lock = LockFile(
        plugins=[
            LockEntry(
                name="db",
                entrypoint="arc.plugins.db.plugin:DatabasePlugin",
                provides=["db.engine", "db.session"],
                requires=[],
                load_order=0,
                critical=True,
            ),
            LockEntry(
                name="api",
                entrypoint="arc.plugins.api.plugin:ApiPlugin",
                provides=["http.router"],
                requires=["db.session"],
                load_order=50,
                critical=True,
            ),
            LockEntry(
                name="http",
                entrypoint="arc.plugins.http.plugin:HttpPlugin",
                provides=["http.app"],
                requires=[],
                load_order=90,
                critical=True,
            ),
        ]
    )
    PluginLoader.write_lock(root / "arc.lock", lock)
    (root / "arc.toml").write_text(_DEFAULT_TOML.format(name=name), encoding="utf-8")
    _echo(f"✓ Initialised Arc project at {root}")
    _echo("  arc.lock  — db, api, http (all ordinary plugins)")
    _echo("  arc.toml  — app config")
    _echo("\nNext: set DATABASE_URL, then `arc new-plugin <name>` and `arc db migrate`.")


# ── new-plugin ───────────────────────────────────────────────────────────
@app.command("new-plugin")
def new_plugin(name: str = typer.Argument(..., help="Plugin name, e.g. finance")) -> None:
    """Scaffold {name}/ and register it in arc.lock."""
    try:
        lock_path = find_lock_file()
    except Exception:
        _echo("arc.lock not found — run `arc init` first.")
        raise typer.Exit(1)

    root = lock_path.parent
    base = root / name
    if base.exists():
        _echo(f"Directory '{base}' already exists.")
        raise typer.Exit(1)

    raw = json.loads(lock_path.read_text(encoding="utf-8"))
    lock = LockFile.model_validate(raw)
    if any(e.name == name for e in lock.plugins):
        _echo(f"Plugin '{name}' already registered.")
        raise typer.Exit(1)

    cls = name.capitalize() + "Plugin"
    for sub in ("schemas", "resources", "api", "patches", "tests"):
        (base / sub).mkdir(parents=True)
    (base / "__init__.py").write_text(f'"""Arc plugin: {name}"""\n')
    (base / "plugin.py").write_text(_PLUGIN_STUB.format(name=name, cls=cls))
    (base / "resources" / "__init__.py").write_text("")
    (base / "patches" / "__init__.py").write_text("")

    lock.plugins.append(
        LockEntry(
            name=name,
            entrypoint=f"{name}.plugin:{cls}",
            requires=["db.session"],
            load_order=100,
        )
    )
    PluginLoader.write_lock(lock_path, lock)
    _echo(f"✓ Scaffolded {base} and registered it in arc.lock")
    _echo(f"  Add schemas to {name}/schemas/  → arc db migrate")
    _echo(f"  Add patches to {name}/patches/  → arc db migrate (runs patches too)")


# ── run ──────────────────────────────────────────────────────────────────
@app.command()
def run(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
) -> None:
    """Build the app and serve it with uvicorn.

    Single-worker by design (uvicorn receives an app object). For multiple
    workers or --reload, serve the import-string entrypoint instead:
        uvicorn arc.asgi:app --workers 4
    """
    from arc.kernel.orchestrator import Arc

    Arc.shared().run(host=host, port=port)


# ── doctor ───────────────────────────────────────────────────────────────
@app.command()
def doctor() -> None:
    """Resolve the plugin graph and print startup order + capabilities."""
    from arc.kernel.orchestrator import Arc

    arc = Arc.shared()
    assert arc.graph is not None
    _echo("Resolved plugin order:")
    for i, p in enumerate(arc.graph.order):
        flags = []
        if p.critical:
            flags.append("critical")
        if p.provides:
            flags.append("provides=" + ",".join(p.provides))
        if p.requires:
            flags.append("requires=" + ",".join(p.requires))
        suffix = ("  [" + " · ".join(flags) + "]") if flags else ""
        _echo(f"  {i}. {p.name} v{p.version}{suffix}")
    _echo(f"\nExtension points populated: {', '.join(arc.extensions.points()) or '(none)'}")


# ── clear-cache ───────────────────────────────────────────────────────────
@app.command("clear-cache")
def clear_cache() -> None:
    """Clear all Arc runtime caches (jinja, query cache, import cache)."""
    import importlib

    cleared: list[str] = []

    # 0. the process-wide shared Arc (so the next command rebuilds fresh)
    try:
        from arc.kernel.orchestrator import Arc
        Arc.reset_shared()
        cleared.append("shared Arc instance")
    except Exception:
        pass

    # 1. structlog processor cache (reset forces re-configuration on next log call)
    try:
        import structlog
        structlog.reset_defaults()
        cleared.append("structlog processor cache")
    except Exception:
        pass

    # 2. Python import cache for user plugins and arc.plugins.*
    # Removes any arc.plugins.* and user plugin modules so the next
    # `arc run` / `arc doctor` re-imports them fresh from disk.
    import sys
    stale = [k for k in sys.modules if k.startswith("arc.plugins") or _is_user_plugin(k)]
    for key in stale:
        del sys.modules[key]
    if stale:
        cleared.append(f"import cache ({len(stale)} modules)")

    # 3. Invalidate importlib's path caches (picks up newly installed packages)
    importlib.invalidate_caches()
    cleared.append("importlib path cache")

    # 4. __pycache__ dirs inside registered plugins (optional — uncomment if needed)
    # try:
    #     root = find_lock_file().parent
    #     for d in root.rglob("__pycache__"):
    #         shutil.rmtree(d, ignore_errors=True)
    #     cleared.append("__pycache__ dirs")
    # except Exception:
    #     pass

    if cleared:
        for item in cleared:
            _echo(f"  ✓ {item}")
        _echo("\nCache cleared.")
    else:
        _echo("Nothing to clear.")


def _is_user_plugin(module_name: str) -> bool:
    """True if module_name looks like a top-level user plugin (no dots, not stdlib)."""
    if "." in module_name:
        return False
    stdlib = {"os", "sys", "re", "json", "math", "io", "abc", "typing",
              "pathlib", "asyncio", "logging", "inspect", "importlib"}
    return module_name not in stdlib


# ── plugin command mounting ───────────────────────────────────────────────
def _mount_plugin_commands() -> None:
    from arc.kernel.orchestrator import Arc
    from arc.kernel.registry import Points

    # Outside a project (no arc.lock) there is nothing to mount and nothing
    # to warn about — `arc init` / `arc new-plugin` must work in silence.
    try:
        find_lock_file()
    except Exception:
        return

    try:
        arc = Arc.shared()
    except Exception as exc:
        # A project exists but the build failed (broken plugin, bad lock,
        # bad config). Previously this was log.debug — `arc db` simply
        # vanished from the command list with no explanation.
        typer.echo(f"⚠ Arc build failed — plugin commands unavailable: {exc}", err=True)
        typer.echo("  Run `arc doctor` for details.", err=True)
        log.warning("arc.cli.build_failed", error=str(exc))
        return

    for contributed in arc.extensions.get(Points.CLI_COMMANDS):
        if isinstance(contributed, typer.Typer):
            app.add_typer(contributed)


def main() -> None:
    _mount_plugin_commands()
    app()


_DEFAULT_TOML = """\
[app]
name = "{name}"
version = "0.1.0"
environment = "development"
debug = true

[log]
level = "INFO"
renderer = "console"

[plugins.db]
# url overridden by DATABASE_URL env var
url = "postgresql+asyncpg://arcuser:arcpass@localhost:5432/arcdb"

# Backup / trash settings (optional)
[plugins.db.backup]
encrypt = false                       # true to encrypt backups
encryption_key_env = "ARC_BACKUP_KEY" # env var holding the passphrase
retention_days = 30                   # `arc db cleanup` default cutoff

[plugins.api]
auto_crud = true
"""

_PLUGIN_STUB = '''\
"""Arc plugin: {name}"""
from arc.kernel.plugin import Plugin
from arc.kernel.contracts import CheckResult


class {cls}(Plugin):
    requires = ("db.session",)
    load_order = 100

    @property
    def name(self) -> str:
        return "{name}"

    @property
    def version(self) -> str:
        return "1.0.0"

    def contribute(self, rt) -> None:
        # Contribute db schema sources, api resources, or cli commands here.
        pass

    async def health_check(self) -> CheckResult:
        return CheckResult.ok()
'''


if __name__ == "__main__":
    main()