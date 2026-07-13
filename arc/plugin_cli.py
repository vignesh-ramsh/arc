"""
arc.plugin_cli
-------------------
Lets a plugin contribute its own `arc <plugin-name> ...` subcommands —
`arc psqldb connect`, `arc psqldb status`, and so on — the same way
`register(kernel)` lets it contribute a capability. Kernel stays
domain-blind (§3.1): this file never knows "psqldb" or "redix" by name, it
just discovers and mounts whatever Typer app a plugin chooses to expose.

A plugin opts in via a SECOND, separate entry-point group in its own
pyproject.toml (distinct from `arc.plugins`, which is register() only):

    [project.entry-points."arc.plugins.cli"]
    psqldb = "psqldb.cli:app"

...where the target is a `typer.Typer()` instance (or a zero-arg callable
returning one). It is mounted under the plugin's own name, so whatever
subcommands that Typer app defines become `arc psqldb <subcommand>`.

Scope rule: a plugin's CLI is mounted if it appears in the CURRENT
project's plugins.lock at all — regardless of enabled/disabled. Disabling
a plugin only affects arc.boot() (§3.6, runtime attachment); it must not
hide operational/debug tooling like `connect`/`status`, which people
reasonably want to use *while* a plugin is disabled (e.g. mid-migration).

Failure handling: a broken or misconfigured plugin CLI must never break
the base `arc` tool (`arc --help` has to keep working). Every failure mode
here is a skip-with-a-printed-warning, never a raised exception — mirrors
the advisory philosophy already used in arc.resolver / arc.kernel.advise.
"""

from __future__ import annotations

from importlib.metadata import entry_points as _installed_entry_points
import sys
from typing import Any
import typer
from rich.console import Console

from . import registry
from .runtime import find_project_root

PLUGIN_CLI_ENTRY_POINT_GROUP = "arc.plugins.cli"

# Top-level commands the kernel itself owns — a plugin can never claim these.
RESERVED_CLI_NAMES = frozenset({"init", "install", "build", "settings", "plugin", "doctor", "health"})

console = Console()


def mount_plugin_clis(app: typer.Typer) -> None:
    """
    Best-effort: mount every project plugin's CLI Typer app onto `app`, if it
    has one. A silent no-op outside a project (e.g. the global bootstrap
    `arc` used only for `init`/`install` before any project exists) — there
    is no plugins.lock to consult yet, so there is nothing to mount.
    """
    root = find_project_root()  # None outside a project; never raises/exits here
    if root is None:
        return

    lock_path = root / ".arc" / "plugins.lock"
    if not lock_path.exists():
        return

    lock_doc = registry.load_lock(lock_path)
    known_plugin_names = {name for name, _entry in registry.list_plugins(lock_doc)}
    mounted: set[str] = set()

    for ep in _installed_entry_points(group=PLUGIN_CLI_ENTRY_POINT_GROUP):
        if ep.name not in known_plugin_names:
            continue  # not part of this project — same "stray, ignore" spirit as the resolver

        if ep.name in RESERVED_CLI_NAMES:
            console.print(
                f"[yellow]Skipping CLI for plugin '{ep.name}': that name is "
                f"reserved for a built-in `arc` command.[/yellow]"
            )
            continue
        if ep.name in mounted:
            console.print(
                f"[yellow]Skipping duplicate CLI entry point for plugin "
                f"'{ep.name}' — already mounted.[/yellow]"
            )
            continue

        plugin_app = _load_plugin_cli_app(ep)
        if plugin_app is None:
            continue

        app.add_typer(plugin_app, name=ep.name, help=f"Commands for the '{ep.name}' plugin.")
        mounted.add(ep.name)


def _load_plugin_cli_app(ep: Any) -> typer.Typer | None:
    try:
        target = ep.load()
    except Exception as exc:
        console.print(
            f"[yellow]Could not import CLI for plugin '{ep.name}': "
            f"{exc.__class__.__name__}: {exc}\n"
            f"  This `arc` is running under: {sys.executable}\n"
            f"  If that's not this project's .venv/bin/python, you're likely running "
            f"the global bootstrap `arc` instead of the project-local one (§3.8) — "
            f"try the full path to this project's own arc, e.g. `./.venv/bin/arc`, "
            f"or `hash -r` if you activated .venv after `arc` was already resolved "
            f"in this shell.[/yellow]"
        )
        return None

    plugin_app = target() if callable(target) and not isinstance(target, typer.Typer) else target

    if not isinstance(plugin_app, typer.Typer):
        console.print(
            f"[yellow]Plugin '{ep.name}'s {PLUGIN_CLI_ENTRY_POINT_GROUP} entry "
            f"point must resolve to a typer.Typer app (or a zero-arg callable "
            f"returning one) — got {type(plugin_app).__name__}. Skipped.[/yellow]"
        )
        return None
    return plugin_app