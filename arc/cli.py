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

import asyncio
import os
import re
import secrets as stdlib_secrets
import shutil
import subprocess
import sys
import warnings
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


_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _plugin_template_files(name: str) -> dict[str, str]:
    """Every file `arc new-plugin` writes, keyed by path relative to
    plugins/<name>/. hooks/api/tasks get real .py files with the whole
    example commented out line-by-line — loaded for real via
    register_hooks/register_api/register_tasks the moment the plugin
    boots, so an uncommented decorator would immediately do something
    (register a live hook, a real HTTP-reachable endpoint, a real
    background job) — not what a starter file should do by default.
    schemas/patches get a README instead of a same-shape sample .json:
    JSON has no comment syntax, so a real .json file there would be
    loaded as an ACTUAL schema by psqldb.register_model() the moment
    this plugin boots, silently creating a real table nobody asked for."""
    return {
        ".gitignore": "__pycache__/\n",
        "plugin.toml": (
            "[plugin]\n"
            f'name = "{name}"\n'
            'version = "0.1.0"\n'
            f'capability = "{name}"\n'
            'requires = ["psqldb", "relay"]\n'
            'optional_requires = ["gateway"]\n'
        ),
        "pyproject.toml": (
            "[project]\n"
            f'name = "{name}"\n'
            'version = "0.1.0"\n'
            f'description = "ARC business plugin: {name}"\n'
            'requires-python = ">=3.12"\n'
            "dependencies = []\n"
            "\n"
            '[project.entry-points."arc.plugins"]\n'
            f"{name} = \"{name}:register\"\n"
            "\n"
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n'
            "\n"
            "[tool.hatch.build.targets.wheel]\n"
            f'packages = ["{name}"]\n'
        ),
        f"{name}/__init__.py": (
            f'"""{name} — an ARC business plugin.\n'
            "\n"
            "Scaffolded by `arc new-plugin`. docs/arc.MD §3.9 (schemas/patches),\n"
            "§3.11 (hooks/api/tasks), §3.7 (directory conventions) cover how each\n"
            'of the directories below gets loaded.\n"""\n'
            "\n"
            "from pathlib import Path\n"
            "from typing import Any\n"
            "\n"
            f'CAPABILITY = "{name}"\n'
            "\n"
            "\n"
            "def register(kernel: Any) -> None:\n"
            '    psqldb = kernel.get("psqldb")\n'
            '    psqldb.register_model(Path(__file__).parent / "schemas")\n'
            '    psqldb.register_patches(Path(__file__).parent / "patches")\n'
            "\n"
            '    relay = kernel.get("relay")\n'
            '    relay.register_hooks(Path(__file__).parent / "hooks")\n'
            '    relay.register_api(Path(__file__).parent / "api")\n'
            '    relay.register_tasks(Path(__file__).parent / "tasks")\n'
            "\n"
            "    # Serve this plugin's own UI, once you've built one — see ui/README.md.\n"
            '    # if kernel.has("gateway"):\n'
            '    #     ui_dist = Path(__file__).parent / "ui" / "dist"\n'
            "    #     if ui_dist.is_dir():\n"
            f'    #         kernel.get("gateway").mount_spa(ui_dist, prefix="{name}_desk")\n'
            "\n"
            "    kernel.export(\n"
            f'        CAPABILITY, object(), requires=["psqldb", "relay"], optional_requires=["gateway"]\n'
            "    )\n"
        ),
        f"{name}/schemas/README.md": (
            "# schemas/\n\n"
            "One JSON file per table this plugin OWNS (creates). Loaded via "
            "`psqldb.register_model(...)` in `__init__.py`. The filename (minus "
            "`.json`) becomes the table's file **stem** — the only valid value for "
            "a REFERENCE/TABLE field's `target` elsewhere, never the physical, "
            "slugified table name (docs/arc.MD §3.9).\n\n"
            'Every normal table must declare at least one business `"unique": true` '
            "field of its own (not just the auto-injected `id`).\n\n"
            "Example — `Department.json`:\n\n"
            "```json\n"
            "{\n"
            '  "system": false,\n'
            '  "audit": false,\n'
            '  "child": false,\n'
            '  "fields": [\n'
            '    {"id": "AA01", "name": "code", "type": "STRING", "unique": true, "required": true, "length": 8},\n'
            '    {"id": "AA02", "name": "dept_name", "type": "STRING", "required": true, "length": 100}\n'
            "  ],\n"
            '  "index": [{"key": "idx_dept_name", "fields": ["dept_name"]}]\n'
            "}\n"
            "```\n\n"
            "After adding or changing a schema file:\n"
            "1. `arc psqldb plan` — preview the diff, never touches the DB.\n"
            "2. `arc psqldb migrate` — apply it (run this yourself).\n"
        ),
        f"{name}/patches/README.md": (
            "# patches/\n\n"
            "Add or modify fields YOU own on a table — your own, or another "
            "installed plugin's. Same JSON shape as `schemas/`, minus "
            "`system`/`audit`/`child`. Never create a table here — that's "
            "`schemas/`'s job.\n\n"
            'A patch can\'t target a `"system": true` table (skipped with a '
            "warning at plan/migrate time — docs/arc.MD §3.9).\n\n"
            "Example — `Employee.json` (adding a field to a table some other "
            "installed plugin owns):\n\n"
            "```json\n"
            "{\n"
            '  "fields": [\n'
            '    {"id": "AB01", "name": "emergency_contact", "type": "STRING", "length": 100}\n'
            "  ]\n"
            "}\n"
            "```\n"
        ),
        f"{name}/hooks/example.py": (
            '"""hooks/<Table Name>.py — one file per table, named exactly after '
            "its schema (docs/arc.MD §3.11). Loaded via relay.register_hooks(...).\n\n"
            "Delete this file, or rename it to a real table and uncomment what "
            'you need. Nothing below runs until you do.\n"""\n'
            "\n"
            "# import arc\n"
            "#\n"
            "# @arc.relay.validate\n"
            "# async def check_something(ctx) -> None:\n"
            "#     if ctx.doc.some_field is None:\n"
            '#         arc.relay.throw("some_field is required", code="missing_field")\n'
            "#\n"
            "# @arc.relay.after_save\n"
            "# async def on_saved(ctx) -> None:\n"
            "#     if ctx.doc._is_new:\n"
            '#         arc.relay.log(f"created {ctx.new[\'id\']}")\n'
        ),
        f"{name}/api/example.py": (
            '"""api/*.py — whitelisted functions, not table-named (docs/arc.MD '
            "§3.11). Loaded via relay.register_api(...). Always callable directly "
            "via arc.relay.call(...); additionally reachable over HTTP at "
            "/api/method/<plugin>.<function_name> when gateway is installed.\n\n"
            "Delete this file, or rename it and uncomment what you need.\n"
            '"""\n'
            "\n"
            "# import arc\n"
            "#\n"
            '# @arc.relay.whitelist(methods=["GET"], roles=["Guest"])\n'
            "# async def ping() -> dict:\n"
            '#     return {"ok": True}\n'
        ),
        f"{name}/tasks/example.py": (
            '"""tasks/*.py — background/scheduled jobs (docs/arc.MD §3.11/'
            "§3.15). Loaded via relay.register_tasks(...). Durable + schedulable "
            "when the `lineup` plugin is installed; still runs in-process (just "
            "not durably) if it isn't — never depend on `lineup` directly, "
            "arc.relay.task/enqueue handle that automatically.\n\n"
            "Delete this file, or rename it and uncomment what you need.\n"
            '"""\n'
            "\n"
            "# import arc\n"
            "#\n"
            '# @arc.relay.task(queue="default")\n'
            "# async def send_something(employee_code: str) -> None:\n"
            "#     ...\n"
            "#\n"
            '# @arc.relay.task(queue="low", cron="0 2 * * *")\n'
            "# async def nightly_cleanup() -> None:\n"
            "#     ...\n"
        ),
        f"{name}/ui/README.md": (
            "# ui/\n\n"
            "Most plugins don't need their own UI — this is a placeholder, not "
            "a requirement. If you want to serve one (following admin's own "
            "`/admin-desk` pattern, docs/arc.MD §3.14/§6):\n\n"
            "1. `npm create vite@latest . -- --template react-ts` in this directory.\n"
            "2. Build it, then in `__init__.py`'s `register()`, uncomment the "
            "`mount_spa` block and point it at your own build's `dist/`.\n"
            "3. Pick a route prefix that isn't your plugin's own name if you "
            f'don\'t want the two coupled — e.g. `"{name}_desk"` (already the '
            "default in the commented-out sample above).\n\n"
            "See `plugins/admin/admin/ui/` for a complete, working reference.\n"
        ),
    }


@app.command(name="new-plugin")
def new_plugin(
    name: str = typer.Argument(..., help="Plugin name — becomes plugins/<name>/ and its capability name."),
) -> None:
    """Scaffolds a new plugin directory: plugin.toml, pyproject.toml, and
    the standard schemas/patches/hooks/api/tasks/ui layout (docs/arc.MD
    §3.7/§3.9/§3.11), each with a README or a fully commented-out example
    showing the convention — nothing in it is live until you uncomment
    and adapt it. Initializes its own git repo (main branch, matching
    every other plugin — §3.6) but does not commit anything; runs `uv
    sync --all-packages` and registers it in plugins.lock (enabled by
    default), the same way `arc install` finishes a clone."""
    if not _PLUGIN_NAME_RE.match(name):
        err_console.print(
            f"'{name}' isn't a valid plugin name — must start with a lowercase letter "
            f"and contain only lowercase letters, digits, and underscores (no leading "
            f"underscore either — uv's own packaging tooling rejects distribution names "
            f"that start with one, docs/arc.MD §3.7)."
        )
        raise typer.Exit(code=1)

    root = find_project_root()
    target = root / "plugins" / name
    if target.exists():
        err_console.print(f"plugins/{name} already exists.")
        raise typer.Exit(code=1)

    for rel_path, content in _plugin_template_files(name).items():
        full_path = target / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    run(["git", "init", "-b", "main"], cwd=target)
    console.print(f"[dim]Initialized a new git repo at plugins/{name} — nothing committed yet.[/dim]")

    console.print("Installing dependencies via the workspace...")
    run(["uv", "sync", "--all-packages"], cwd=root)

    manifest = registry.read_manifest(target / "plugin.toml")
    lock_path = root / ".arc" / "plugins.lock"
    lock_doc = registry.load_lock(lock_path)
    lock_doc = registry.merge_manifests_into_lock(lock_doc, [manifest])
    registry.save_lock(lock_path, lock_doc)

    console.print(f"[bold green]Scaffolded '{name}' at plugins/{name} and enabled it.[/bold green]")
    console.print(
        f"[dim]Next: add a schema (see plugins/{name}/{name}/schemas/README.md), "
        f"then `arc psqldb plan` / `arc psqldb migrate` when you're ready to create tables. "
        f"Review and commit plugins/{name} yourself when you're happy with it.[/dim]"
    )


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
# arc clear-cache
# --------------------------------------------------------------------------- #
@app.command(name="clear-cache")
def clear_cache() -> None:
    """Clears everything genuinely cache-like: relay's generic cache
    (cache_get/cache_set/cache_delete) and authn's session/access-key
    cache. Deliberately does NOT touch lineup's job queues or redix's
    rate-limit counters, which live in the same Redis instance under
    their own prefixes — clearing those would drop pending work or reset
    brute-force protection, neither of which "clear the cache" should do.

    A narrow, acknowledged exception to the kernel's own domain-blindness
    (§3.1): this needs to know redix's/authn's own key-prefix conventions
    by name (hardcoded below), which the kernel doesn't otherwise know or
    care about. Kept here anyway because "clear the cache" is a
    cross-cutting operational concern no single plugin owns — the same
    reasoning `arc doctor`/`arc health` already lean on."""
    import arc

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", arc.ArcAdvisory)
        try:
            kernel = arc.boot()
        except arc.BootError as exc:
            err_console.print(str(exc))
            raise typer.Exit(code=1)

    if not kernel.has("redix"):
        console.print("[dim]redix isn't installed — there's no cache to clear.[/dim]")
        return

    # relay's own generic cache: "cache:*". authn's session/access-key
    # cache: "session:*" / "access_key:*" (docs/arc.MD §3.13). Never
    # "lineup:*" (job queues, §3.15) or "ratelimit:*" (redix.rate_limit()).
    _CACHE_KEY_PATTERNS = ("cache:*", "session:*", "access_key:*")

    async def _run() -> int:
        await arc.redix.open()
        try:
            total = 0
            for pattern in _CACHE_KEY_PATTERNS:
                total += await arc.redix.scan_delete(pattern)
            return total
        finally:
            await arc.redix.close()

    total = asyncio.run(_run())
    console.print(
        f"[bold green]Cleared {total} cache key(s)[/bold green] "
        f"(relay's generic cache + authn's session/access-key cache)."
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
def plugin_disable(
    name: str,
    wipe: bool = typer.Option(
        False, "--wipe", help="Also DROP every table this plugin owns from the database. Irreversible."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="With --wipe: proceed even if another plugin has patched fields onto, or a live "
        "REFERENCE into, one of these tables (destroys that too — see the command's own help).",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the drop confirmation prompt (--wipe only)."),
) -> None:
    """Disable a plugin. It is fully unregistered — arc.boot() skips it entirely,
    and its capability namespace (arc.<name>) will not exist at runtime.

    --wipe additionally DROPs every table this plugin owns — a real DROP
    TABLE, not the recoverable soft-delete `arc psqldb clear` uses (a
    dropped table has no _trash entry, only a dropped ROW does). Two
    real risks, handled two different ways: if another still-enabled
    plugin has PATCHED extra fields onto one of these tables, that's
    checked directly (Postgres has no concept of "which plugin owns this
    column") and refused unless --force. If another plugin's table has a
    live REFERENCE pointing at one of these, Postgres's own FK constraint
    refuses the plain DROP TABLE on its own — --force retries with
    CASCADE in that case. Either way, --force means real, additional data
    loss beyond this plugin's own tables — read the preview before
    confirming."""
    root = find_project_root()
    lock_path = root / ".arc" / "plugins.lock"
    lock_doc = registry.load_lock(lock_path)

    if name not in lock_doc.get("plugins", {}):
        err_console.print(f"Plugin '{name}' is not in plugins.lock. Run `arc build` first.")
        raise typer.Exit(code=1)

    if wipe:
        import arc

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", arc.ArcAdvisory)
            try:
                arc.boot()
            except arc.BootError as exc:
                err_console.print(str(exc))
                raise typer.Exit(code=1)

        async def _run() -> bool:
            await arc.psqldb.open()
            try:
                plan = await arc.psqldb.wipe_plugin_tables(name, force=force, dry_run=True)
                all_tables = plan["tables"] + ([plan["audit_table"]] if plan["audit_table"] else [])
                if not all_tables:
                    console.print(f"[dim]'{name}' owns no tables — nothing to wipe.[/dim]")
                    return True
                console.print(f"[bold red]About to PERMANENTLY DROP {len(all_tables)} table(s):[/bold red]")
                for t in all_tables:
                    console.print(f"  {t} ({plan['row_counts'].get(t, 0)} row(s))")
                if not yes and not typer.confirm("This cannot be undone. Proceed?", default=False):
                    console.print("[dim]Aborted — nothing dropped, plugin not disabled.[/dim]")
                    return False
                await arc.psqldb.wipe_plugin_tables(name, force=force, dry_run=False)
                console.print(f"[green]Dropped {len(all_tables)} table(s).[/green]")
                return True
            finally:
                await arc.psqldb.close()

        try:
            proceed = asyncio.run(_run())
        except Exception as exc:
            err_console.print(str(exc))
            raise typer.Exit(code=1)
        if not proceed:
            raise typer.Exit(code=1)

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