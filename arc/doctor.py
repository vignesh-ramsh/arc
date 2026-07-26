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

--deep — the one thing a pure dry run structurally cannot show, because it
never imports plugin code or opens a connection: does register() actually
succeed, is the DB really reachable, what does live pool saturation look
like, and which settings have drifted from their declared defaults. Opt-in
and clearly separate from the default dry run above — it really does boot
(imports every plugin for real) and briefly opens every capability that
exposes open()/close(), then tears it all back down before returning.

Deliberately NOT shown here: arc.events' handler failure counters
(arc.events.stats()). Those live in-process, per-Kernel — a `doctor --deep`
invocation is always a brand-new process with a brand-new kernel that has
seen zero events, so it could only ever report "0 failures", forever,
regardless of what your real running application has actually seen. That
would be worse than not showing it at all. Read arc.events.stats() from
INSIDE your running application instead (e.g. your own health endpoint or
admin page) — same posture as arc.events.subscriptions(), which nothing
here shows either, for the same reason.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from . import resolver
from .kernel import ArcAdvisory
from .registry import load_lock
from .runtime import BootError, find_project_root

console = Console()
err_console = Console(stderr=True, style="bold red")


def doctor(
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of tables."
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help=(
            "Also really boot() (imports every plugin) and briefly open "
            "every capability to report live health, pool stats, and "
            "settings drift. Slower and has side effects (opens real "
            "connections); plain `arc doctor` never does either."
        ),
    ),
) -> None:
    """
    Dry-run boot resolution: show what arc.boot() would load, skip, and warn
    about — or why it would fail — without importing any plugin code.
    Pass --deep to additionally boot for real and report live health/pool
    stats/settings drift.
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
        output = plan.to_dict()
        if deep:
            output["deep"] = _run_deep()
        print(json.dumps(output, indent=2))
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

    if deep:
        _print_deep(_run_deep())


def _fail(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, indent=2))
    else:
        err_console.print(f"Boot would FAIL: {message}")
    raise typer.Exit(code=1)


# ------------------------------------------------------------------------ #
# --deep — real boot, real (brief) connections, always torn back down.
# ------------------------------------------------------------------------ #
def _run_deep() -> dict[str, Any]:
    from . import health as _health
    from .runtime import boot as _boot
    from .runtime import shutdown as _shutdown

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ArcAdvisory)
            kernel = _boot()
    except BootError as exc:
        return {"boot_ok": False, "error": str(exc)}

    async def _open_and_collect() -> dict[str, Any]:
        opened: list[Any] = []
        try:
            for _name, cap in kernel.capabilities().items():
                open_fn = getattr(cap.instance, "open", None)
                if callable(open_fn):
                    await open_fn()
                    opened.append(cap.instance)
            return {
                "boot_ok": True,
                "health": await _health.check(),
                "settings_drift": _settings_drift(),
            }
        finally:
            for instance in reversed(opened):
                close_fn = getattr(instance, "close", None)
                if callable(close_fn):
                    await close_fn()

    try:
        return asyncio.run(_open_and_collect())
    finally:
        _shutdown()


def _settings_drift() -> dict[str, dict[str, Any]]:
    """Type-declared, non-secret settings whose current value (coerced,
    the same way arc.settings.get() would return it) differs from the
    default the owning plugin declared. Secrets are skipped entirely —
    list_all() never reveals a secret value, so there's nothing safe to
    compare. Untyped/never-declared keys are skipped too — there's no
    declared default to drift from."""
    from . import settings as _settings

    drift: dict[str, dict[str, Any]] = {}
    for key, info in _settings.list_all().items():
        if info["kind"] != "setting" or info["type"] is None or info["value"] is None:
            continue
        current = _settings.get(key, reveal=True)
        if current != info["default"]:
            drift[key] = {"value": current, "default": info["default"], "doc": info["doc"]}
    return drift


def _print_deep(deep: dict[str, Any]) -> None:
    console.print()
    console.print("[bold]--deep: real boot + live checks[/bold]")
    if not deep.get("boot_ok"):
        err_console.print(f"Deep check FAILED: {deep.get('error')}")
        raise typer.Exit(code=1)

    health = deep.get("health", {})
    if not health:
        console.print("[dim]No capability exposes health().[/dim]")
    else:
        table = Table(title="capability health")
        table.add_column("capability")
        table.add_column("ok")
        table.add_column("detail")
        for name, result in health.items():
            ok = result.get("ok", True)
            detail = result.get("error") or ", ".join(
                f"{k}={v}" for k, v in result.items() if k not in ("ok", "error")
            )
            table.add_row(name, "[green]yes[/green]" if ok else "[red]NO[/red]", detail)
        console.print(table)

    drift = deep.get("settings_drift", {})
    if not drift:
        console.print(
            "[dim]No settings drift — every typed setting matches its declared default.[/dim]"
        )
    else:
        table = Table(title="settings drift (current value differs from declared default)")
        table.add_column("key")
        table.add_column("value")
        table.add_column("default")
        for key, info in sorted(drift.items()):
            table.add_row(key, str(info["value"]), str(info["default"]))
        console.print(table)
