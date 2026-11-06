"""
arc.status
-----------------
`arc status` — what this project's deployment is supposed to be running, what is
actually running, and where the two disagree. One fast, read-only command; it does
not boot the kernel.

Three independent sources, deliberately never mixed up:

  * EXPECTED  the unit files `arc deploy` wrote (arc.deploy.read_deployments). Those,
              not the settings, are what systemd will start, so they are the truth about
              what "should" be running.
  * RUNNING   systemd's view of each unit (state, restart count) plus the process
              registry `arc ps` lists (a web worker is a process of its own, not a unit).
  * SETTINGS  what `arc deploy` would produce today. If that differs from the units, the
              operator changed a setting and has not applied it yet — reported as drift.

Exit status is 1 when anything is wrong (a failed unit, a count that doesn't match, drift),
0 when everything matches — so it can sit in a monitoring check.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from . import deploy, sizing
from .runtime import find_project_root
from .settings import SettingsManager

console = Console()
err_console = Console(stderr=True, style="bold red")

#: Settings an operator uses to size a deployment (same keys arc.cli reads).
GATEWAY_WORKERS_KEY = "gateway_workers"
LINEUP_QUEUES_KEY = "lineup_queues"
PER_QUEUE_KEY = "lineup_workers_per_queue"
QUEUE_PREFIX = "lineup_workers_"


@dataclass(frozen=True)
class UnitState:
    name: str
    active_state: str = "unknown"  # active | inactive | failed | activating | ...
    sub_state: str = ""
    restarts: int = 0
    main_pid: int = 0
    result: str = ""

    @property
    def running(self) -> bool:
        return self.active_state == "active" and self.main_pid > 0


@dataclass
class Row:
    name: str
    expected: int
    running: int
    restarts: int
    source: str

    @property
    def ok(self) -> bool:
        return self.expected == self.running


@dataclass
class Report:
    base: str
    scope: str  # "user" | "system"
    mode: str  # "dev" | "prod"
    target: str
    target_active: bool
    restart_command: str | None
    rows: list[Row]
    units_total: int
    units_active: int
    units_failed: int
    restarts_total: int
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and all(r.ok for r in self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment": {
                "base": self.base,
                "scope": self.scope,
                "mode": self.mode,
                "target": self.target,
                "target_active": self.target_active,
                "restart_command": self.restart_command,
            },
            "processes": [{**asdict(r), "ok": r.ok} for r in self.rows],
            "units": {
                "total": self.units_total,
                "active": self.units_active,
                "failed": self.units_failed,
                "restarts": self.restarts_total,
            },
            "problems": self.problems,
            "ok": self.ok,
        }


# --------------------------------------------------------------------------- #
# pure: facts in, report out
# --------------------------------------------------------------------------- #
def _explicit(mgr: SettingsManager, key: str) -> int | None:
    """A worker count the operator set; None if unset OR not a usable number (the
    deploy command refuses those; here it just means 'not an explicit setting')."""
    raw = mgr.get(key)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def desired_plan(
    mgr: SettingsManager, mode: str, deployed_queues: list[str]
) -> tuple[sizing.WorkerPlan, dict[str, str]]:
    """What `arc deploy` would size things at today, and where each number comes from.
    The queue list is the `lineup_queues` setting when set, else the deployed one (finding
    out what the plugins declare would need a boot; `arc doctor --deep` does that)."""
    configured = mgr.get(LINEUP_QUEUES_KEY)
    queues = (
        list(dict.fromkeys(q.strip() for q in configured.split(",") if q.strip()))
        if configured
        else list(deployed_queues)
    )
    gateway = _explicit(mgr, GATEWAY_WORKERS_KEY)
    per_queue = _explicit(mgr, PER_QUEUE_KEY)
    overrides = {q: n for q in queues if (n := _explicit(mgr, QUEUE_PREFIX + q)) is not None}
    plan = sizing.plan_workers(
        mode=mode, queues=queues, gateway=gateway, per_queue=per_queue, queue_overrides=overrides
    )
    default = "dev default" if mode == "dev" else "sized from this machine"
    sources = {"gateway": f"setting {GATEWAY_WORKERS_KEY}" if gateway else default}
    for q in queues:
        if q in overrides:
            sources[f"queue {q}"] = f"setting {QUEUE_PREFIX}{q}"
        elif per_queue:
            sources[f"queue {q}"] = f"setting {PER_QUEUE_KEY}"
        else:
            sources[f"queue {q}"] = default
    return plan, sources


def build_report(
    deployment: deploy.Deployment,
    states: dict[str, UnitState],
    registry: Counter,
    *,
    desired: sizing.WorkerPlan | None,
    desired_has_queue_list: bool,
    sources: dict[str, str],
    log_writer_answers: bool,
    restart_command: str | None,
) -> Report:
    mode = "dev" if deployment.scope.name == "user" else "prod"
    unit = lambda u: states.get(u.name, UnitState(u.name))  # noqa: E731
    rows: list[Row] = []
    problems: list[str] = []
    apply_cmd = f"arc deploy {mode}"

    gateway = next((u for u in deployment.units if u.role == "gateway"), None)
    if gateway is not None:
        rows.append(
            Row(
                "gateway (web workers)",
                gateway.workers or 0,
                registry.get("gateway-worker", 0),
                unit(gateway).restarts,
                sources.get("gateway", ""),
            )
        )

    queue_order = list(dict.fromkeys(u.queue for u in deployment.units if u.role == "worker" and u.queue))
    if desired is not None:  # show them in the order the operator configured, not the file system's
        configured = [q for q in desired.per_queue if q in queue_order]
        queue_order = configured + [q for q in queue_order if q not in configured]
    for queue in queue_order:
        members = [u for u in deployment.units if u.role == "worker" and u.queue == queue]
        rows.append(
            Row(
                f"queue {queue}",
                len(members),
                sum(1 for u in members if unit(u).running),
                sum(unit(u).restarts for u in members),
                sources.get(f"queue {queue}", ""),
            )
        )

    scheduler = next((u for u in deployment.units if u.role == "scheduler"), None)
    if scheduler is not None:
        rows.append(Row("scheduler", 1, registry.get("lineup-scheduler", 0), unit(scheduler).restarts, "fixed (one only)"))

    logs = next((u for u in deployment.units if u.role == "logs"), None)
    if logs is not None:
        rows.append(
            Row("log writer", 1, 1 if unit(logs).running and log_writer_answers else 0, unit(logs).restarts, "fixed")
        )

    # --- things systemd says are wrong
    for u in deployment.units:
        st = unit(u)
        if st.active_state == "failed":
            ctl = " ".join(deployment.scope.ctl)
            problems.append(
                f"{u.name} has FAILED ({st.result or 'see journalctl'}) — after fixing the cause: "
                f"{ctl} reset-failed {u.name} && {ctl} start {u.name}"
            )
        elif st.active_state == "activating":
            problems.append(f"{u.name} is restarting ({st.sub_state or 'activating'}).")
    target_state = states.get(deployment.target, UnitState(deployment.target))
    if not target_state.active_state == "active":
        problems.append(
            f"{deployment.target} is {target_state.active_state}: nothing starts on boot, and "
            f"`{restart_command or 'arc restart'}` will not bring the units back."
        )

    # --- registry vs units: processes nobody deployed (a leftover `arc run`, a second copy)
    unit_workers = sum(r.running for r in rows if r.name.startswith("queue "))
    registered_workers = registry.get("lineup-worker", 0)
    if registered_workers != unit_workers:
        problems.append(
            f"`arc ps` lists {registered_workers} lineup worker process(es) but {unit_workers} worker "
            f"unit(s) are running — something else is running workers too (a leftover `arc run`?)."
        )

    # --- settings vs units: the operator changed something and hasn't applied it
    if desired is not None:
        if gateway is not None and desired.gateway != (gateway.workers or 0):
            problems.append(
                f"settings give {desired.gateway} web worker(s) but the units run {gateway.workers} "
                f"— run `{apply_cmd}` to apply."
            )
        deployed = {q: sum(1 for u in deployment.units if u.role == "worker" and u.queue == q) for q in queue_order}
        for queue, count in desired.per_queue.items():
            if queue not in deployed:
                problems.append(f"queue {queue} is configured but has no worker unit — run `{apply_cmd}`.")
            elif deployed[queue] != count:
                problems.append(
                    f"queue {queue}: settings give {count} worker(s) but the units run {deployed[queue]} "
                    f"— run `{apply_cmd}` to apply."
                )
        if desired_has_queue_list:
            for queue in deployed:
                if queue not in desired.per_queue:
                    problems.append(
                        f"the units run workers for queue {queue}, which is no longer in "
                        f"{LINEUP_QUEUES_KEY} — run `{apply_cmd}` to remove them."
                    )

    states_of_units = [unit(u) for u in deployment.units]
    return Report(
        base=deployment.base,
        scope=deployment.scope.name,
        mode=mode,
        target=deployment.target,
        target_active=target_state.active_state == "active",
        restart_command=restart_command,
        rows=rows,
        units_total=len(deployment.units),
        units_active=sum(1 for s in states_of_units if s.active_state == "active"),
        units_failed=sum(1 for s in states_of_units if s.active_state == "failed"),
        restarts_total=sum(s.restarts for s in states_of_units),
        problems=problems,
    )


# --------------------------------------------------------------------------- #
# the thin layer that gathers the facts
# --------------------------------------------------------------------------- #
def read_unit_states(scope: deploy.Scope, names: list[str]) -> dict[str, UnitState] | None:
    """systemd's view of each unit, or None if systemctl can't be asked."""
    try:
        result = subprocess.run(
            [
                *scope.ctl, "show", "-p", "Id", "-p", "ActiveState", "-p", "SubState",
                "-p", "NRestarts", "-p", "MainPID", "-p", "Result", *names,
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    states: dict[str, UnitState] = {}
    for block in result.stdout.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" not in kv:
            continue
        states[kv["Id"]] = UnitState(
            name=kv["Id"],
            active_state=kv.get("ActiveState", "unknown"),
            sub_state=kv.get("SubState", ""),
            restarts=int(kv.get("NRestarts") or 0),
            main_pid=int(kv.get("MainPID") or 0),
            result=kv.get("Result", ""),
        )
    return states


def _log_writer_answers(project_root: Any) -> bool:
    from . import log as arc_log
    from .log_collector import default_socket_path

    return arc_log._collector_reachable(str(default_socket_path(project_root)))


def render(report: Report) -> None:
    scope_text = "per-user systemd units" if report.scope == "user" else "system-wide systemd units"
    console.print(f"[bold]{report.base}[/bold] — {scope_text}, {report.mode} sizing")
    if report.restart_command:
        console.print(f"[dim]restart: {report.restart_command}[/dim]")
    table = Table()
    table.add_column("process")
    table.add_column("expected", justify="right")
    table.add_column("running", justify="right")
    table.add_column("restarts", justify="right")
    table.add_column("configured by")
    for r in report.rows:
        running = f"[green]{r.running}[/green]" if r.ok else f"[bold red]{r.running}[/bold red]"
        table.add_row(r.name, str(r.expected), running, str(r.restarts) if r.restarts else "-", r.source)
    console.print(table)
    failed = f", [bold red]{report.units_failed} failed[/bold red]" if report.units_failed else ", 0 failed"
    console.print(
        f"units: {report.units_active} of {report.units_total} active{failed} · "
        f"restarts since they started: {report.restarts_total}"
    )
    for problem in report.problems:
        console.print(f"[bold yellow]![/bold yellow] {problem}")
    if report.ok:
        console.print("[bold green]Everything matches.[/bold green]")


def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """What this project's deployment should be running, what is, and where they differ:
    web workers, one worker set per queue, the scheduler, the log writer — with unit
    health and restart counts. Read-only and fast (it does not boot the kernel). Exits 1
    when anything is wrong, so it can be used in a monitoring check."""
    from . import events

    root = find_project_root()
    if root is None:
        err_console.print("Not an ARC project (no .arc/arc.toml found here or in any parent).")
        raise typer.Exit(code=1)
    registry = Counter(p.get("role", "?") for p in events.list_processes(root))
    deployments = deploy.read_deployments(root)

    if not deployments:
        if as_json:
            print(json.dumps({"deployment": None, "running": dict(registry)}, indent=2))
            return
        console.print(
            "This project isn't deployed with `arc deploy` (no units found), so there is no "
            "expected state to compare against."
        )
        if registry:
            console.print("Running now (from `arc ps`): " + ", ".join(f"{r} × {n}" for r, n in sorted(registry.items())))
        else:
            console.print("Nothing is running. `arc deploy dev` installs and supervises the processes.")
        return

    states_by_scope = {
        d.scope.name: read_unit_states(d.scope, [d.target, *(u.name for u in d.units)]) for d in deployments
    }
    # Normally exactly one deployment; with two, report the one that is actually up.
    chosen = next((d for d in deployments if (states_by_scope[d.scope.name] or {}).get(d.target, UnitState(d.target)).active_state == "active"), deployments[0])
    states = states_by_scope[chosen.scope.name]

    mgr = SettingsManager(root / ".arc")
    mode = "dev" if chosen.scope.name == "user" else "prod"
    deployed_queues = list(dict.fromkeys(u.queue for u in chosen.units if u.role == "worker" and u.queue))
    desired, sources = desired_plan(mgr, mode, deployed_queues) if deployed_queues or any(
        u.role == "gateway" for u in chosen.units
    ) else (None, {})
    report = build_report(
        chosen,
        states or {},
        registry,
        desired=desired,
        desired_has_queue_list=bool(mgr.get(LINEUP_QUEUES_KEY)),
        sources=sources,
        log_writer_answers=_log_writer_answers(root),
        restart_command=mgr.get("restart_command"),
    )
    if states is None:
        report.problems.append(
            f"could not ask systemd about the units (`{' '.join(chosen.scope.ctl)} show` failed), "
            f"so the 'running' numbers for unit-backed processes are unknown."
        )
    if len(deployments) > 1:
        others = ", ".join(f"{d.base} ({d.scope.name})" for d in deployments if d is not chosen)
        report.problems.append(f"more than one deployment exists for this project; also found: {others}.")

    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        render(report)
    if not report.ok:
        raise typer.Exit(code=1)
