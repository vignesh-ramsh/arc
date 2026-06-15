"""
cli_commands.py  —  paste these into arc/kernel/cli.py
======================================================
This file is NOT imported anywhere. It is the source for the command changes
to apply to arc/kernel/cli.py. Replace the existing `init`, `run`, and
`new-plugin` commands with the versions below, and add `install`, `enable`,
`disable`. The existing `doctor`, `clear-cache`, `_mount_plugin_commands`,
and `main` stay as they are.

Top-of-file imports to add:

    from arc.kernel import installer
    from arc.kernel.state import LocalState

(`json`, `sys`, `Path`, `typer`, `LockEntry`, `LockFile`, `PluginLoader`,
`find_lock_file` are already imported.)
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from arc.kernel import installer
from arc.kernel.loader import LockEntry, LockFile, PluginLoader, find_lock_file
from arc.kernel.state import LocalState
from arc.kernel.logger import get_logger

log = get_logger(__name__)
app = typer.Typer(name="arc", help="Arc — everything is a plugin.", no_args_is_help=True)


def _echo(msg: str) -> None:
    typer.echo(msg)


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_gitignore(root: Path, entries: list[str]) -> None:
    """Append entries to root .gitignore if absent. Idempotent."""
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    have = {ln.strip() for ln in existing}
    missing = [e for e in entries if e not in have]
    if not missing:
        return
    block = (["", "# Arc — local, per-machine (do not commit)"] if existing else
             ["# Arc — local, per-machine (do not commit)"]) + missing
    with gi.open("a", encoding="utf-8") as f:
        f.write(("\n" if existing else "") + "\n".join(block) + "\n")


def _project_root_or_exit() -> Path:
    try:
        return find_lock_file().parent
    except Exception:
        _echo("arc.lock not found — run `arc init` first.")
        raise typer.Exit(1)


# ── init  (REPLACES existing init — now purely empty) ─────────────────────────
@app.command()
def init(
    directory: str = typer.Argument(".", help="Project directory"),
    name: str = typer.Option("arc-app", help="App name"),
) -> None:
    """Create an EMPTY Arc project. Install plugins yourself with `arc install`.

    Creates (all per-machine, all gitignored):
      arc.lock         — empty plugin set
      arc.toml         — app config only
      plugins/         — tracked dir, contents ignored (.gitignore inside)
      .arc/            — local state / cache
    Idempotent: existing files are left untouched.
    """
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)

    lock_path = root / "arc.lock"
    if not lock_path.exists():
        PluginLoader.write_lock(lock_path, LockFile(plugins=[]))

    toml_path = root / "arc.toml"
    if not toml_path.exists():
        toml_path.write_text(_EMPTY_TOML.format(name=name), encoding="utf-8")

    # plugins/ tracked, contents ignored.
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(exist_ok=True)
    pgi = plugins_dir / ".gitignore"
    if not pgi.exists():
        pgi.write_text("*\n!.gitignore\n", encoding="utf-8")

    (root / ".arc" / "state").mkdir(parents=True, exist_ok=True)

    # Make sure the local-only files are never committed by default.
    _ensure_gitignore(root, ["/arc.lock", "/arc.toml", "/.arc/"])

    _echo(f"✓ Initialised empty Arc project at {root}")
    _echo("  arc.lock / arc.toml / .arc/  — gitignored (per-machine)")
    _echo("  plugins/                     — tracked dir, contents gitignored")
    _echo("\nNext: install plugins, e.g.")
    _echo("  arc install https://github.com/you/arc-psqldb --branch main")


# ── install  (NEW) ────────────────────────────────────────────────────────────
@app.command()
def install(
    url: str = typer.Argument(..., help="Git URL of the plugin repo"),
    branch: str = typer.Option("main", "--branch", "-b", help="Branch / tag"),
    disabled: bool = typer.Option(False, "--disabled", help="Install but leave disabled"),
    force: bool = typer.Option(False, "--force", help="Replace if already installed"),
) -> None:
    """Clone a plugin into plugins/<name>, install its deps, register it."""
    root = _project_root_or_exit()
    try:
        entry = installer.install_from_git(url, branch, project_root=root, force=force)
    except installer.InstallerError as exc:
        _echo(f"✗ install failed: {exc}")
        raise typer.Exit(1)

    if disabled:
        LocalState(root).disable(entry.name)
        _echo(f"✓ Installed plugins/{entry.name} (left disabled)")
    else:
        _echo(f"✓ Installed plugins/{entry.name}  ({(entry.commit or '')[:8]})")
    _echo("  Run `arc doctor` to verify the graph.")


# ── enable / disable  (NEW, local-only, idempotent) ──────────────────────────
@app.command()
def enable(name: str = typer.Argument(..., help="Plugin name")) -> None:
    """Enable a plugin on THIS machine (local, not committed)."""
    root  = _project_root_or_exit()
    state = LocalState(root)

    if not state.is_disabled(name):
        _echo(f"'{name}' is already enabled — nothing to do.")
        return

    missing = installer.unsatisfied_after_enable(
        name, project_root=root, already_disabled=state.disabled_set()
    )
    state.enable(name)
    _echo(f"✓ Enabled '{name}' locally.")
    if missing:
        _echo(f"  ⚠ It requires {', '.join(missing)}, not provided by any enabled "
              f"plugin. Enable/install a provider or it won't start.")


@app.command()
def disable(name: str = typer.Argument(..., help="Plugin name")) -> None:
    """Disable a plugin on THIS machine (local, not committed)."""
    root  = _project_root_or_exit()
    state = LocalState(root)

    if state.is_disabled(name):
        _echo(f"'{name}' is already disabled — nothing to do.")
        return

    blockers = installer.disable_blockers(
        name, project_root=root, already_disabled=state.disabled_set()
    )
    if blockers:
        _echo(f"✗ Cannot disable '{name}' — other plugins depend on it:")
        for plugin, cap in blockers:
            _echo(f"    {plugin} requires '{cap}' (only {name} provides it)")
        _echo("  Disable those first, or install an alternative provider.")
        raise typer.Exit(1)

    state.disable(name)
    _echo(f"✓ Disabled '{name}' locally. (Run `arc run` to apply.)")


# ── run  (REPLACES existing run — adds --reload, dev-gated) ───────────────────
@app.command()
def run(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    reload: bool = typer.Option(False, "--reload", help="Dev auto-reload on plugins/ changes"),
) -> None:
    """Build the app and serve it with uvicorn."""
    from arc.kernel.orchestrator import Arc

    arc = Arc.shared()

    if arc.graph is not None and not arc.graph.order:
        _echo("No plugins installed (or all disabled). "
              "Install one: `arc install <git-url>`.")
        raise typer.Exit(1)

    if reload:
        env = arc.config.app.environment if arc.config else "production"
        if env != "development":
            _echo(f"--reload ignored: environment is '{env}', not 'development'.")
        else:
            from arc.kernel.watcher import run_with_reload
            root = find_lock_file().parent
            run_with_reload(host=host, port=port, project_root=root)
            return

    arc.run(host=host, port=port)


# ── new-plugin  (REPLACES existing — scaffolds under plugins/<name>) ──────────
@app.command("new-plugin")
def new_plugin(name: str = typer.Argument(..., help="Plugin name, e.g. finance")) -> None:
    """Scaffold plugins/{name}/ and register it in arc.lock."""
    root = _project_root_or_exit()
    base = root / "plugins" / name
    if base.exists():
        _echo(f"Directory '{base}' already exists.")
        raise typer.Exit(1)

    lock_path = root / "arc.lock"
    lock = LockFile.model_validate(json.loads(lock_path.read_text(encoding="utf-8")))
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
    # A local plugin still needs a manifest so `arc install`/others see it.
    (base / "plugin.toml").write_text(
        _MANIFEST_STUB.format(name=name, cls=cls), encoding="utf-8"
    )

    lock.plugins.append(
        LockEntry(
            name=name,
            entrypoint=f"plugins.{name}.plugin:{cls}",
            requires=["db.session"],
            load_order=100,
        )
    )
    PluginLoader.write_lock(lock_path, lock)
    _echo(f"✓ Scaffolded {base} and registered it in arc.lock")

# ── doctor ───────────────────────────────────────────────────────────────────
@app.command()
def doctor() -> None:
    """Resolve the plugin graph and print startup order + capabilities."""
    from arc.kernel.orchestrator import Arc
 
    try:
        arc = Arc.shared()
    except Exception as exc:
        typer.echo(f"✗ Build failed: {exc}")
        raise typer.Exit(1)
 
    if arc.graph is None or not arc.graph.order:
        typer.echo("No plugins installed or all disabled.")
        typer.echo("Install one:  arc install <git-url> --branch main")
        return
 
    typer.echo("Resolved plugin order:")
    for i, p in enumerate(arc.graph.order):
        flags = []
        if p.critical:
            flags.append("critical")
        if p.provides:
            flags.append("provides=" + ",".join(p.provides))
        if p.requires:
            flags.append("requires=" + ",".join(p.requires))
        suffix = ("  [" + " · ".join(flags) + "]") if flags else ""
        typer.echo(f"  {i}. {p.name}{suffix}")
 
    # Show locally-disabled plugins so the operator can see the full picture.
    from arc.kernel.state import LocalState
    from arc.kernel.loader import find_lock_file
    try:
        root     = find_lock_file().parent
        disabled = LocalState(root).disabled_set()
        if disabled:
            typer.echo("\nLocally disabled (this machine only):")
            for name in sorted(disabled):
                typer.echo(f"  – {name}")
    except Exception:
        pass
 
 
# ── clear-cache ───────────────────────────────────────────────────────────────
@app.command("clear-cache")
def clear_cache() -> None:
    """Reset import cache, structlog cache and importlib path cache."""
    import importlib
    import sys
 
    removed = []
 
    # 1. Drop user-plugin modules so a re-import picks up file changes.
    to_drop = [
        k for k in sys.modules
        if k.startswith("plugins.")
        or (
            "." not in k
            and k not in {
                "os", "sys", "re", "json", "math", "io", "abc", "typing",
                "pathlib", "asyncio", "logging", "inspect", "importlib",
            }
        )
    ]
    for key in to_drop:
        del sys.modules[key]
        removed.append(key)
 
    # 2. Reset importlib path cache so new plugin directories are found.
    importlib.invalidate_caches()
 
    # 3. Reset structlog's bound-processor cache (if structlog is loaded).
    try:
        import structlog
        structlog.reset_defaults()
    except Exception:
        pass
 
    if removed:
        typer.echo(f"Cleared {len(removed)} module(s) from import cache.")
    else:
        typer.echo("Nothing to clear.")


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

# ── templates ─────────────────────────────────────────────────────────────────

_EMPTY_TOML = """\
[app]
name = "{name}"
version = "0.1.0"
environment = "development"
debug = true

[log]
level = "INFO"
renderer = "console"

# Install plugins with `arc install <git-url>`.
# Add per-plugin config under [plugins.<name>] as needed.
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
        pass

    async def health_check(self) -> CheckResult:
        return CheckResult.ok()
'''

_MANIFEST_STUB = """\
name = "{name}"
version = "1.0.0"
entrypoint = "plugin:{cls}"
provides = []
requires = ["db.session"]
load_order = 100
critical = false

[python]
dependencies = []
"""

if __name__ == "__main__":
    main()