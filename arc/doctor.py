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
from rich.markup import escape
from rich.table import Table

from . import permissions, resolver, settings as arc_settings
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
    fix: bool = typer.Option(
        False,
        "--fix",
        help=(
            "Make `.arc/` and the settings store private to this user (mode 700/600) if other "
            "users on this machine can read them - which they can in a project created before "
            "that became the default. The only thing `arc doctor` ever changes."
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

    # arc.boot() refuses a secrets provider ARC does not have; a dry run that says "OK" must too.
    try:
        arc_settings.read_secrets_provider(root / ".arc" / "arc.toml")
    except arc_settings.SettingsError as exc:
        _fail(str(exc), as_json)
        return

    lock_doc = load_lock(root / ".arc" / "plugins.lock")
    try:
        plan = resolver.resolve(root, lock_doc=lock_doc)
    except resolver.ResolutionError as exc:
        _fail(str(exc), as_json)
        return

    exposed, repaired, unrepaired = _check_permissions(root / ".arc", fix)

    if as_json:
        output = plan.to_dict()
        output["permissions"] = {
            "exposed": [f.to_dict() for f in exposed],
            "fixed": [f.to_dict() for f in repaired],
            "failed": [{**f.to_dict(), "reason": reason} for f, reason in unrepaired],
        }
        if deep:
            output["deep"] = _run_deep()
        print(json.dumps(output, indent=2))
        if unrepaired:
            raise typer.Exit(code=1)
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
    _print_permissions(root / ".arc", exposed, repaired, unrepaired, fix)

    if deep:
        _print_deep(_run_deep())
    if unrepaired:
        raise typer.Exit(code=1)


def _check_permissions(
    arc_dir: Any, fix: bool
) -> tuple[list[permissions.Finding], list[permissions.Finding], list[tuple[permissions.Finding, str]]]:
    """(what was exposed, what `--fix` repaired, what it could not). Read-only without --fix."""
    exposed = permissions.audit(arc_dir)
    if not fix or not exposed:
        return exposed, [], []
    repaired, unrepaired = permissions.fix(exposed)
    return exposed, repaired, unrepaired


def _print_permissions(
    arc_dir: Any,
    exposed: list[permissions.Finding],
    repaired: list[permissions.Finding],
    unrepaired: list[tuple[permissions.Finding, str]],
    fix: bool,
) -> None:
    if not exposed:
        console.print("[dim]Permissions: .arc/ and the settings store are private to this user.[/dim]")
        return
    names = {f.path: f"{arc_dir.parent.name}/.arc" + ("" if f.path == arc_dir else f"/{f.path.name}") for f in exposed}
    for finding in repaired:
        console.print(
            f"[green]fixed:[/green] {names[finding.path]}  {finding.mode:03o} -> {finding.wanted:03o}"
        )
    for finding, reason in unrepaired:
        err_console.print(f"could not fix {names[finding.path]} ({finding.mode:03o}): {reason}")
    if fix:
        return
    for finding in exposed:
        console.print(
            f"[yellow]warning:[/yellow] {names[finding.path]} is mode {finding.mode:03o}: "
            f"{finding.why}. It should be {finding.wanted:03o}."
        )
    if all(f.fixable for f in exposed):
        console.print("Run `arc doctor --fix` to set them.")
    else:
        console.print(
            "Some are owned by another user; run `arc doctor --fix` as that user (or with sudo)."
        )


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
                "queue_coverage": _queue_coverage(kernel),
            }
        finally:
            for instance in reversed(opened):
                close_fn = getattr(instance, "close", None)
                if callable(close_fn):
                    await close_fn()

    try:
        result = asyncio.run(_open_and_collect())
        # After the loop above has finished: asking PostgreSQL uses a loop of its own.
        result["connections"] = _connections(kernel)
        result["cpu_warnings"] = _cpu_warnings(kernel)
        return result
    finally:
        _shutdown()


def _connections(kernel: Any) -> dict[str, Any] | None:
    """PostgreSQL's connection limit against what this project's processes can open: the pool
    this process got and where that number came from, the server's limits, what is open, and
    what is wrong with the deployment's plan (if it was deployed with `arc deploy`)."""
    if not kernel.has("pgdb"):
        return None
    from . import deploy, status
    from .settings import SettingsManager

    pg = kernel.get("pgdb")
    deployments = deploy.read_deployments(kernel.project_root)
    expected = status.expected_processes(deployments[0]) if deployments else {}
    mode = "dev" if deployments and deployments[0].scope.name == "user" else "prod"
    info, problems = status.gather_connections(
        SettingsManager(kernel.project_root / ".arc"), expected, apply_command=f"arc deploy {mode}"
    )
    return {
        "this_process": {"role": pg.role, "pool_max": pg.max_size, "from": pg.size_source},
        **(info.to_dict() if info is not None else {}),
        "problems": problems,
    }


def cpu_demand_warnings(demands: list[dict[str, Any]], web_workers: int, cores: int) -> list[str]:
    """One warning for each plugin whose CPU demand, over every web worker, could take more than half of
    the machine's cores. A demand is `{"what", "threads" (per web worker), "advice"}` as a plugin's
    `web_worker_cpu_demand()` gives it. Half, because the database, nginx and the job workers share the
    machine: a flood that reaches it should still leave them room."""
    if web_workers <= 0:
        return []
    warnings_out: list[str] = []
    for demand in demands:
        total = web_workers * int(demand["threads"])
        if total > cores / 2:
            warnings_out.append(
                f"{demand['what']} can use up to {total} threads at once ({web_workers} web workers x "
                f"{demand['threads']}) on a machine with {cores} cores. {demand['advice']}"
            )
    return warnings_out


def _cpu_demands(kernel: Any) -> list[dict[str, Any]]:
    """What each capability that states one (`web_worker_cpu_demand()`) says it may spend per web worker."""
    demands: list[dict[str, Any]] = []
    for _name, cap in kernel.capabilities().items():
        ask = getattr(cap.instance, "web_worker_cpu_demand", None)
        if not callable(ask):
            continue
        try:
            demand = ask()
        except Exception:  # noqa: BLE001 - a plugin that cannot answer must not stop the rest of the check
            continue
        if demand:
            demands.append(demand)
    return demands


def _cpu_warnings(kernel: Any) -> list[str]:
    """For a project deployed with `arc deploy`: the demands against the web workers and cores it has.
    Nothing when it was not deployed that way, since the number of web workers is then unknown."""
    from . import deploy, sizing, status

    deployments = deploy.read_deployments(kernel.project_root)
    if not deployments:
        return []
    web_workers = status.expected_processes(deployments[0]).get(sizing.ROLE_GATEWAY, 0)
    return cpu_demand_warnings(_cpu_demands(kernel), web_workers, sizing.detect_cpu_count())


def _queue_coverage(kernel: Any) -> dict[str, list[str]] | None:
    """For a project deployed with `arc deploy`: which of the queues its plugins declare
    have a worker unit. A queue without one is never consumed — jobs for it just wait,
    silently, because the units were written before the plugin that added the queue.
    None when lineup isn't installed or the project wasn't deployed that way."""
    if not kernel.has("lineup"):
        return None
    from . import deploy

    deployed = deploy.deployed_worker_queues(kernel.project_root)
    if deployed is None:
        return None
    declared = list(kernel.get("lineup").queues())
    return {
        "declared": declared,
        "covered": sorted(deployed),
        "missing": [q for q in declared if q not in deployed],
    }


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

    connections = deep.get("connections")
    if connections:
        mine = connections["this_process"]
        console.print(
            f"[dim]Database pool of this process: {mine['pool_max']} ({mine['from']}). "
            + (
                f"PostgreSQL lets this application open {connections['usable']}, "
                f"{connections['in_use']} are open now.[/dim]"
                if connections.get("usable") is not None
                else "PostgreSQL could not be asked.[/dim]"
            )
        )
        for problem in connections["problems"]:
            err_console.print(f"[bold yellow]warning:[/bold yellow] {problem}")

    for warning in deep.get("cpu_warnings") or []:
        err_console.print(f"[bold yellow]warning:[/bold yellow] {escape(warning)}")

    coverage = deep.get("queue_coverage")
    if coverage and coverage["missing"]:
        err_console.print(
            f"[bold yellow]warning:[/bold yellow] queue(s) {', '.join(coverage['missing'])} have no "
            f"worker unit, so jobs for them are never consumed. Run `arc deploy dev` (or `arc "
            f"deploy prod`) again to add them."
        )
    elif coverage:
        console.print(
            f"[dim]Every queue has a worker unit: {', '.join(coverage['declared'])}.[/dim]"
        )

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
