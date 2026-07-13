"""
arc.healthcmd
-----------------
`arc health` — a CLI view onto arc.health.check(), the same way `arc doctor`
is a CLI view onto arc.resolver.resolve(). Named healthcmd.py (not health.py)
so it never collides with the real arc.health module this wraps.

Opens every capability that has an open() first (short-lived — same idea as
gateway's own ASGI lifespan open/close, just scoped to one CLI invocation
instead of a running server) so a check actually exercises real
connections, not just "constructed, never started".
"""

from __future__ import annotations

import asyncio
import json
import warnings

import typer
from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True, style="bold red")


def health(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON instead of a table."),
) -> None:
    """Boot, open every capability, run arc.health.check() across all of
    them, then close back down. Exits non-zero if anything reported unhealthy."""
    import arc

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", arc.ArcAdvisory)
        try:
            kernel = arc.boot()
        except arc.BootError as exc:
            _fail(str(exc), as_json)
            return  # unreachable; _fail raises typer.Exit

    results = asyncio.run(_check(kernel))

    if as_json:
        print(json.dumps({"ok": arc.health.all_ok(results), "results": results}, indent=2, default=str))
    else:
        table = Table(title="arc.health.check()")
        table.add_column("capability")
        table.add_column("ok")
        table.add_column("detail")
        for name, result in sorted(results.items()):
            ok = bool(result.get("ok", True))
            detail = ", ".join(f"{k}={v}" for k, v in result.items() if k != "ok")
            table.add_row(name, "[green]yes[/green]" if ok else "[bold red]no[/bold red]", detail)
        console.print(table)

    if not arc.health.all_ok(results):
        raise typer.Exit(code=1)


async def _check(kernel) -> dict:
    import arc

    opened = []
    for name, cap in kernel.capabilities().items():
        open_fn = getattr(cap.instance, "open", None)
        if callable(open_fn):
            await open_fn()
            opened.append(cap.instance)
    try:
        return await arc.health.check()
    finally:
        for instance in opened:
            close_fn = getattr(instance, "close", None)
            if callable(close_fn):
                await close_fn()


def _fail(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": message}, indent=2))
    else:
        err_console.print(f"Health check FAILED: {message}")
    raise typer.Exit(code=1)
