"""
arc.cli
--------------
The `arc` command. Subcommands implemented here:

    arc init [project_name] [--env dev|staging|prod]
    arc build [-p/--plugin NAME] [--no-lock]
    arc settings get <key> [--reveal]
    arc settings set <key> <value> [--secret]
    arc settings delete <key>
    arc plugin enable <name>
    arc plugin disable <name>
    arc plugin list
    arc doctor [--json]
"""

from __future__ import annotations

import os
import secrets as stdlib_secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import tomlkit
import typer
from rich.console import Console
from rich.table import Table

from . import registry
from .doctor import doctor as _doctor_command
from .healthcmd import health as _health_command
from .plugin_cli import mount_plugin_clis
from .settings import REDACTED, SettingsError, SettingsManager

app = typer.Typer(name="arc", help="ARC kernel CLI", no_args_is_help=True)
settings_app = typer.Typer(help="Get, set, or delete a setting.", no_args_is_help=True)
plugin_app = typer.Typer(help="Enable, disable, or list plugins.", no_args_is_help=True)
app.add_typer(settings_app, name="settings")
app.add_typer(plugin_app, name="plugin")
app.command(name="doctor")(_doctor_command)
app.command(name="health")(_health_command)
mount_plugin_clis(app)

console = Console()
err_console = Console(stderr=True, style="bold red")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def find_project_root(start: Path | None = None) -> Path:
    """Walk upward from `start` (default: cwd) looking for a .arc/arc.toml."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".arc" / "arc.toml").exists():
            return candidate
    err_console.print(
        "Not an ARC project (no .arc/arc.toml found in this directory or any parent). "
        "Run `arc init` first."
    )
    raise typer.Exit(code=1)


def run(cmd: list[str], cwd: Path) -> None:
    console.print(f"[dim]$ {' '.join(cmd)}[/dim]")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        err_console.print(f"Command failed: {' '.join(cmd)}")
        raise typer.Exit(code=result.returncode)


# --------------------------------------------------------------------------- #
# arc init
# --------------------------------------------------------------------------- #
@app.command()
def init(
    project_name: str = typer.Argument(
        None, help="Directory to create. Defaults to the current directory."
    ),
    env: str = typer.Option("dev", "--env", help="Default ARC_ENV for this project."),
    kernel_repo: str = typer.Option(
        os.environ.get("ARC_KERNEL_REPO", ""),
        "--kernel-repo",
        help="Git URL (or local path) to clone the kernel from into arc/. "
             "Can also be set via the ARC_KERNEL_REPO env var.",
    ),
    kernel_branch: str = typer.Option("main", "--kernel-branch", help="Branch/tag to clone."),
) -> None:
    """
    Scaffold a new ARC instance: arc/ (kernel, git-cloned), plugins/, .arc/,
    config/, logs/, backups/ — and wire it all up as a single uv workspace.
    """
    root = Path(project_name).resolve() if project_name else Path.cwd()
    root.mkdir(parents=True, exist_ok=True)

    arc_dir = root / ".arc"
    (arc_dir / "runtime").mkdir(parents=True, exist_ok=True)

    for d in ["plugins", "config", "logs", "backups/db", "backups/files"]:
        (root / d).mkdir(parents=True, exist_ok=True)
    for keep in ["logs", "backups/db", "backups/files"]:
        (root / keep / ".gitkeep").touch(exist_ok=True)

    # --- kernel source: clone arc/ if it doesn't already exist -------------
    kernel_dir = root / "arc"
    if kernel_dir.exists():
        console.print(f"[yellow]{kernel_dir} already exists — leaving untouched.[/yellow]")
    elif kernel_repo:
        run(["git", "clone", "--branch", kernel_branch, kernel_repo, str(kernel_dir)], cwd=root)
    else:
        err_console.print(
            "No --kernel-repo given and ARC_KERNEL_REPO is not set. "
            "The kernel source has nothing to clone from.\n"
            "Pass --kernel-repo <git-url-or-path>, or set ARC_KERNEL_REPO once "
            "in your shell profile so every `arc init` picks it up automatically."
        )
        raise typer.Exit(code=1)

    # --- master key ----------------------------------------------------
    mkey_path = arc_dir / "arc.mkey"
    if mkey_path.exists():
        console.print(f"[yellow]{mkey_path} already exists — leaving untouched.[/yellow]")
    else:
        mkey_path.write_text(stdlib_secrets.token_hex(32))
        mkey_path.chmod(0o600)
        console.print(f"[green]Generated master key: {mkey_path}[/green]")

    # --- empty secrets store --------------------------------------------
    secrets_path = arc_dir / "arc.secrets"
    if not secrets_path.exists():
        secrets_path.touch()
        secrets_path.chmod(0o600)
        console.print(f"[green]Created empty secrets store: {secrets_path}[/green]")

    # --- arc.toml --------------------------------------------------------
    toml_path = arc_dir / "arc.toml"
    if toml_path.exists():
        console.print(f"[yellow]{toml_path} already exists — leaving untouched.[/yellow]")
    else:
        doc = tomlkit.document()

        project_table = tomlkit.table()
        project_table["name"] = root.name
        project_table["env"] = env
        doc["project"] = project_table

        doc["settings"] = tomlkit.table()

        secrets_section = tomlkit.table()
        secrets_section["provider"] = "local_file"
        secrets_section["declared"] = tomlkit.array()
        doc["secrets"] = secrets_section

        logging_table = tomlkit.table()
        logging_table["level"] = "INFO"
        logging_table["dir"] = "logs"
        doc["logging"] = logging_table

        toml_path.write_text(tomlkit.dumps(doc))
        console.print(f"[green]Wrote default {toml_path}[/green]")

    # --- config/*.toml overlays -------------------------------------------
    for env_name in ["common", "dev", "staging", "prod"]:
        p = root / "config" / f"{env_name}.toml"
        if not p.exists():
            p.write_text(f"# {env_name} environment overrides — merged on top of .arc/arc.toml\n")

    # --- plugins.lock skeleton --------------------------------------------
    lock_path = arc_dir / "plugins.lock"
    if not lock_path.exists():
        registry.save_lock(lock_path, registry.load_lock(lock_path))
        console.print(f"[green]Created empty {lock_path}[/green]")

    # --- .gitignore --------------------------------------------------------
    # arc/ and plugins/*/ are each their OWN independent git repos. If this
    # project root is also a git repo, git would otherwise treat them as
    # "embedded repositories" (silently skipped or added as a dangling
    # gitlink, not real tracked files) — so the outer repo ignores their
    # contents entirely and each is managed via its own remote instead.
    gitignore_entries = [
        ".arc/arc.mkey", ".arc/arc.rkey", ".arc/arc.secrets", ".arc/runtime/",
        "logs/*.log", "backups/db/*", "backups/files/*",
        "!backups/db/.gitkeep", "!backups/files/.gitkeep",
        ".venv/", "__pycache__/",
        "/arc/", "/plugins/*/",
    ]
    gitignore_path = root / ".gitignore"
    existing = gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
    with gitignore_path.open("a") as f:
        for entry in gitignore_entries:
            if entry not in existing:
                f.write(entry + "\n")

    # --- root pyproject.toml: a uv WORKSPACE, not a single package ---------
    # arc/ and every plugins/* are independent packages with their own
    # pyproject.toml; the root just aggregates them into one shared venv
    # and one shared lock file. The root itself ships no code, hence
    # tool.uv.package = false.
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        doc = tomlkit.document()
        project_table = tomlkit.table()
        project_table["name"] = root.name
        project_table["version"] = "0.1.0"
        project_table["requires-python"] = ">=3.12"
        project_table["dependencies"] = tomlkit.array()
        doc["project"] = project_table

        uv_table = tomlkit.table()
        uv_table["package"] = False
        doc["tool"] = tomlkit.table()
        doc["tool"]["uv"] = uv_table

        workspace_table = tomlkit.table()
        workspace_table["members"] = ["arc", "plugins/*"]
        doc["tool"]["uv"]["workspace"] = workspace_table

        pyproject.write_text(tomlkit.dumps(doc))
        console.print(f"[green]Wrote workspace {pyproject}[/green]")

    run(["uv", "sync", "--all-packages"], cwd=root)

    console.print(f"\n[bold green]ARC instance scaffolded at: {root}[/bold green]")
    console.print("[yellow]Using local-file secrets — fine for dev/self-hosted. "
                   "Switch [secrets].provider in .arc/arc.toml for cloud production.[/yellow]")


def _infer_plugin_name(github_url: str) -> str:
    """github.com/org/foo-plugin(.git) -> foo-plugin"""
    path = urlparse(github_url).path if "://" in github_url else github_url
    stem = path.rstrip("/").rsplit("/", 1)[-1]
    return stem[:-4] if stem.endswith(".git") else stem


@app.command()
def install(
    github_url: str = typer.Argument(..., help="Git URL of the plugin repo to clone."),
    branch: str = typer.Option(
        None, "--branch", help="Branch/tag to clone. Defaults to the repo's default branch."
    ),
    name: str = typer.Option(
        None, "--name", help="Directory name under plugins/. Inferred from the URL if omitted."
    ),
) -> None:
    """
    Clone a plugin's git repo into plugins/<name> and register it —
    analogous to `bench get-app`. Installs the plugin's own declared
    Python dependencies via the uv workspace; no network dependency
    resolution beyond the clone itself and a single `uv sync`.
    """
    root = find_project_root()
    plugin_name = name or _infer_plugin_name(github_url)
    target = root / "plugins" / plugin_name

    if target.exists():
        err_console.print(
            f"plugins/{plugin_name} already exists. "
            f"Pass --name to install under a different directory, or remove it first."
        )
        raise typer.Exit(code=1)

    clone_cmd = ["git", "clone"]
    if branch:
        clone_cmd += ["--branch", branch]
    clone_cmd += [github_url, str(target)]
    run(clone_cmd, cwd=root)

    manifest_path = target / "plugin.toml"
    if not manifest_path.exists():
        err_console.print(
            f"{github_url} was cloned into plugins/{plugin_name}, but it has no "
            f"plugin.toml at its root — this doesn't look like an ARC plugin. "
            f"Removing the clone."
        )
        shutil.rmtree(target, ignore_errors=True)
        raise typer.Exit(code=1)

    console.print("Installing dependencies via the workspace...")
    run(["uv", "sync", "--all-packages"], cwd=root)

    manifest = registry.read_manifest(manifest_path)
    lock_path = root / ".arc" / "plugins.lock"
    lock_doc = registry.load_lock(lock_path)
    lock_doc = registry.merge_manifests_into_lock(lock_doc, [manifest])
    registry.save_lock(lock_path, lock_doc)

    console.print(
        f"[bold green]Installed '{plugin_name}' "
        f"(capability: {manifest.capability}) and enabled it.[/bold green]"
    )

    all_manifests = registry.discover_plugins(root / "plugins")
    for w in registry.validate_requires(all_manifests):
        console.print(f"[yellow]Warning: {w}[/yellow]")



@app.command()
def build(
    plugin: str = typer.Option(
        None, "-p", "--plugin", help="Refresh only this plugin's plugins.lock entry."
    ),
    no_lock: bool = typer.Option(
        False, "--no-lock", help="Skip the `uv lock` step (just sync from the existing lock)."
    ),
) -> None:
    """
    Re-resolve and re-install everything currently on disk under arc/ and
    plugins/*, and refresh .arc/plugins.lock to match.

    This does NOT fetch anything from git — that's `arc install`'s job.
    `arc build` is what you run after a fresh clone of the whole project
    (CI, a new machine, restoring from backup), or after hand-editing a
    plugin's own pyproject.toml dependencies.
    """
    root = find_project_root()
    plugins_dir = root / "plugins"
    lock_path = root / ".arc" / "plugins.lock"

    all_manifests = registry.discover_plugins(plugins_dir)
    if not all_manifests:
        console.print("[yellow]No plugins found under plugins/ — nothing to build.[/yellow]")
    else:
        warnings = registry.validate_requires(all_manifests)
        for w in warnings:
            console.print(f"[yellow]Warning: {w}[/yellow]")

    to_refresh = (
        [m for m in all_manifests if m.name == plugin] if plugin else all_manifests
    )
    if plugin and not to_refresh:
        available = ", ".join(m.name for m in all_manifests) or "none"
        err_console.print(f"No plugin named '{plugin}' found. Available: {available}")
        raise typer.Exit(code=1)

    if not no_lock:
        run(["uv", "lock"], cwd=root)
    run(["uv", "sync", "--all-packages"], cwd=root)

    if to_refresh:
        lock_doc = registry.load_lock(lock_path)
        lock_doc = registry.merge_manifests_into_lock(lock_doc, to_refresh)
        registry.save_lock(lock_path, lock_doc)
        console.print(
            f"[bold green]Build complete. Refreshed: "
            f"{', '.join(m.name for m in to_refresh)}[/bold green]"
        )


# --------------------------------------------------------------------------- #
# arc settings get / set / delete
# --------------------------------------------------------------------------- #
@settings_app.command("get")
def settings_get(
    key: str,
    reveal: bool = typer.Option(
        False, "--reveal", help="Show the real value even if the key is a secret."
    ),
) -> None:
    """Get a setting's value. Secret values print as ******** unless --reveal is passed."""
    root = find_project_root()
    mgr = SettingsManager(root / ".arc")
    try:
        value = mgr.get(key, reveal=reveal)
    except SettingsError as exc:
        err_console.print(str(exc))
        raise typer.Exit(code=1)

    if value is None:
        err_console.print(f"'{key}' is not set.")
        raise typer.Exit(code=1)
    console.print(value)


@settings_app.command("set")
def settings_set(
    key: str,
    value: str,
    secret: bool = typer.Option(
        False, "--secret", help="Store this value encrypted and mark the key as secret."
    ),
) -> None:
    """Set a setting. Plain settings go into .arc/arc.toml; --secret routes to arc.secrets."""
    root = find_project_root()
    mgr = SettingsManager(root / ".arc")
    try:
        mgr.set(key, value, secret=secret)
    except SettingsError as exc:
        err_console.print(str(exc))
        raise typer.Exit(code=1)

    shown = REDACTED if secret else value
    console.print(f"[green]Set {key} = {shown}[/green]" + (" (secret)" if secret else ""))


@settings_app.command("delete")
def settings_delete(key: str) -> None:
    """Delete a setting, whether plain or secret."""
    root = find_project_root()
    mgr = SettingsManager(root / ".arc")
    existed = mgr.delete(key)
    if existed:
        console.print(f"[green]Deleted '{key}'.[/green]")
    else:
        err_console.print(f"'{key}' was not set.")
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# arc plugin enable / disable / list
# --------------------------------------------------------------------------- #
@plugin_app.command("enable")
def plugin_enable(name: str) -> None:
    """Enable a plugin. arc.boot() will call its register() and attach its namespace."""
    root = find_project_root()
    lock_path = root / ".arc" / "plugins.lock"
    lock_doc = registry.load_lock(lock_path)
    try:
        registry.set_enabled(lock_doc, name, True)
    except registry.RegistryError as exc:
        err_console.print(str(exc))
        raise typer.Exit(code=1)
    registry.save_lock(lock_path, lock_doc)
    console.print(f"[green]Enabled plugin '{name}'.[/green]")


@plugin_app.command("disable")
def plugin_disable(name: str) -> None:
    """Disable a plugin. It is fully unregistered — arc.boot() skips it entirely,
    and its capability namespace (arc.<name>) will not exist at runtime."""
    root = find_project_root()
    lock_path = root / ".arc" / "plugins.lock"
    lock_doc = registry.load_lock(lock_path)
    try:
        registry.set_enabled(lock_doc, name, False)
    except registry.RegistryError as exc:
        err_console.print(str(exc))
        raise typer.Exit(code=1)
    registry.save_lock(lock_path, lock_doc)
    console.print(f"[yellow]Disabled plugin '{name}'. It will not be loaded by arc.boot().[/yellow]")


@plugin_app.command("list")
def plugin_list() -> None:
    """Show every plugin known to plugins.lock and its enabled/disabled state."""
    root = find_project_root()
    lock_path = root / ".arc" / "plugins.lock"
    lock_doc = registry.load_lock(lock_path)
    entries = registry.list_plugins(lock_doc)

    if not entries:
        console.print("[dim]No plugins in plugins.lock yet. Run `arc build` first.[/dim]")
        return

    table = Table()
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Capability")
    table.add_column("Enabled")
    table.add_column("Requires")
    for name, entry in entries:
        enabled = entry.get("enabled", True)
        table.add_row(
            name,
            str(entry.get("version", "")),
            str(entry.get("capability", "")),
            "[green]yes[/green]" if enabled else "[red]no[/red]",
            ", ".join(entry.get("requires", [])) or "-",
        )
    console.print(table)


if __name__ == "__main__":
    app()