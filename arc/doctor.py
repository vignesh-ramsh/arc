"""
arc.doctor
-----------------
`arc doctor` — a true dry run of arc.boot()'s resolution phase.

Shows exactly what boot WOULD do — load order, skipped plugins, warnings —
or exactly why it would fail, without importing a single line of plugin code
and without starting anything. Powered by the same pure function
(arc.resolver.resolve) that boot() executes, so doctor can never drift from
reality.

Output: rich tables for humans, `--json` for machines (answers the open
"text vs JSON" question with: both).

Wiring into the CLI is two lines in arc/cli.py:

    from .doctor import doctor as _doctor_command
    app.command(name="doctor")(_doctor_command)
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from . import resolver
from .registry import load_lock
from .runtime import BootError, find_project_root

console = Console()
err_console = Console(stderr=True, style="bold red")


def doctor(
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of tables."
    ),
) -> None:
    """
    Dry-run boot resolution: show what arc.boot() would load, skip, and warn
    about — or why it would fail — without importing any plugin code.
    """
    try:
        root = find_project_root()
    except BootError as exc:  # e.g. $ARC_PROJECT_ROOT points somewhere invalid
        _fail(str(exc), as_json)
        return  # unreachable; _fail raises typer.Exit
    if root is None:
        _fail(
            "Not inside an ARC project — no .arc/arc.toml found in the current "
            "directory or any parent.",
            as_json,
        )
        return

    lock_doc = load_lock(root / ".arc" / "plugins.lock")
    try:
        plan = resolver.resolve(root, lock_doc=lock_doc)
    except resolver.ResolutionError as exc:
        _fail(str(exc), as_json)
        return

    if as_json:
        print(json.dumps(plan.to_dict(), indent=2))
        return

    if not plan.load_order:
        console.print(
            "[dim]No enabled plugins — arc.boot() would start with an empty "
            "capability registry.[/dim]"
        )
    else:
        table = Table(title="arc.boot() load order")
        table.add_column("#", justify="right")
        table.add_column("plugin")
        table.add_column("capability")
        table.add_column("version")
        table.add_column("requires")
        table.add_column("optional")
        for position, spec in enumerate(plan.load_order, start=1):
            table.add_row(
                str(position),
                spec.name,
                spec.capability,
                spec.version,
                ", ".join(spec.requires) or "-",
                ", ".join(spec.optional_requires) or "-",
            )
        console.print(table)

    for skipped in plan.skipped:
        console.print(f"[yellow]skipped:[/yellow] {skipped.name} — {skipped.reason}")
    for warning in plan.warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")

    console.print("[bold green]Boot resolution OK.[/bold green]")


def _fail(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, indent=2))
    else:
        err_console.print(f"Boot would FAIL: {message}")
    raise typer.Exit(code=1)