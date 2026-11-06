"""
arc.sizing
-----------------
Resource-aware default worker-process counts for `arc run` (arc/cli.py).
Deliberately CPU/memory-only — no awareness of any specific plugin's own
settings (e.g. pgdb's own connection-pool-size setting): the Kernel
stays domain-blind (§3.1), and this module is Kernel-owned infrastructure,
not pgdb-aware tuning. An operator running a connection-limited Postgres
still needs to size `gateway_workers` themselves via arc.settings if this
default is too high for their DB — this only picks a sane STARTING POINT,
one that beats both a hardcoded number and a naive `2 * cpu_count`
formula blind to memory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_CEILING = 8
_RESERVE_CPU = 1  # leave at least one core for Postgres/Redis/the OS itself
_RESERVE_MEM_GB = 1.0  # leave at least this much memory for everything else
_MEM_PER_WORKER_MB = 150  # a conservative per-process budget (interpreter + a small pool)


def detect_cpu_count() -> int:
    return os.cpu_count() or 1


def detect_memory_gb() -> float:
    """Total physical memory, in GiB. POSIX-portable via os.sysconf (no new
    dependency, unlike psutil) — falls back to a conservative 2 GiB guess
    if the platform doesn't expose these sysconf names at all."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return (pages * page_size) / (1024**3)
    except (ValueError, OSError, AttributeError):
        pass
    return 2.0


def calculate_worker_count(*, ceiling: int = DEFAULT_CEILING) -> int:
    """A sane default worker-process count for THIS machine, never
    exceeding `ceiling`. Two independent budgets — CPU and memory — the
    smaller one wins, since running out of either stalls every worker, not
    just the process count.

    Deliberately NOT a blind `2 * cpu_count + 1` (Gunicorn's own classic
    default): every ARC worker process opens its OWN independent
    connection pool via its own arc.boot() (docs/arc.MD §3.6), so more
    workers directly multiplies live DB/Redis connections — a formula that
    only looks at cores would happily recommend 24 workers on a 12-core
    box and quietly starve Postgres. Memory-awareness is the cheap,
    domain-blind proxy for that risk without the Kernel needing to know
    pgdb's own pool-size setting exists."""
    cpu = detect_cpu_count()
    mem_gb = detect_memory_gb()

    cpu_budget = max(1, cpu - _RESERVE_CPU)
    usable_mem_gb = mem_gb - _RESERVE_MEM_GB
    mem_budget = max(1, int(usable_mem_gb * 1024 // _MEM_PER_WORKER_MB)) if usable_mem_gb > 0 else 1

    return max(1, min(cpu_budget, mem_budget, ceiling))


def describe(*, ceiling: int = DEFAULT_CEILING) -> str:
    """Human-readable one-liner for what calculate_worker_count() decided
    and why — same "show your work" posture `arc pgdb migrate`'s own
    plan already uses, so a computed default is never a silent black box."""
    cpu = detect_cpu_count()
    mem_gb = detect_memory_gb()
    n = calculate_worker_count(ceiling=ceiling)
    return f"{n} (auto: {cpu} CPU core(s), {mem_gb:.1f} GiB RAM detected, ceiling {ceiling})"


# --------------------------------------------------------------------------- #
# What `arc deploy` runs
# --------------------------------------------------------------------------- #
#: `arc deploy dev`: small and predictable. Two web workers, one worker per queue.
DEV_GATEWAY_WORKERS = 2
DEV_WORKERS_PER_QUEUE = 1


@dataclass(frozen=True)
class WorkerPlan:
    gateway: int
    #: queue name -> number of worker processes for that queue
    per_queue: dict[str, int]


def plan_workers(
    *,
    mode: str,
    queues: list[str],
    gateway: int | None = None,
    per_queue: int | None = None,
    queue_overrides: dict[str, int] | None = None,
) -> WorkerPlan:
    """How many processes a deployment should run. `gateway`, `per_queue` and
    `queue_overrides` are the operator's explicit settings; any of them wins over
    the default for its mode and is honoured as given (there is no ceiling on a
    number somebody chose on purpose — but note every process holds its own
    database pool).

    mode "dev":  2 web workers and 1 worker per queue — enough to work against, light
                 on a laptop. Raise them with the settings.
    mode "prod": sized from THIS machine (calculate_worker_count: CPU and memory, never
                 more than the ceiling) — that many web workers, and the same number
                 of job workers in total, shared out evenly over the queues with at
                 least one each.
    """
    if mode == "dev":
        auto_gateway, auto_per_queue = DEV_GATEWAY_WORKERS, DEV_WORKERS_PER_QUEUE
    elif mode == "prod":
        auto = calculate_worker_count()
        auto_gateway, auto_per_queue = auto, max(1, auto // max(1, len(queues)))
    else:
        raise ValueError(f"unknown deployment mode {mode!r}")
    base = per_queue or auto_per_queue
    overrides = queue_overrides or {}
    return WorkerPlan(
        gateway=gateway or auto_gateway,
        per_queue={q: overrides.get(q) or base for q in queues},
    )


# --------------------------------------------------------------------------- #
# Database connection budget
# --------------------------------------------------------------------------- #
# Every ARC process holds its OWN database pool, so the number of connections a
# deployment can open is (processes) x (pool size), summed over roles. PostgreSQL
# refuses connections past its limit, and a pool that cannot get one turns requests
# into errors. The budget is split here, in plain integers; reading the server's
# real limits is the database plugin's job (pgdb.budget).

#: Process roles as `arc ps` lists them (and as ARC_PROCESS_ROLE names them).
ROLE_GATEWAY = "gateway-worker"
ROLE_LINEUP_WORKER = "lineup-worker"
ROLE_LINEUP_SCHEDULER = "lineup-scheduler"

#: What each role would like. The gateway serves many requests at once; a job worker
#: runs as many jobs as it has connections (see lineup's concurrency cap); the scheduler
#: only polls.
POOL_DESIRED = {ROLE_GATEWAY: 10, ROLE_LINEUP_WORKER: 5, ROLE_LINEUP_SCHEDULER: 2}
#: The least a role can work with. Below this a request or job that needs two
#: connections at once (a hook opening a second transaction) can deadlock itself.
POOL_FLOORS = {ROLE_GATEWAY: 2, ROLE_LINEUP_WORKER: 2, ROLE_LINEUP_SCHEDULER: 1}
#: Connections kept free for everything that is not a long-running ARC process:
#: migrations, `arc console`, backups, an administrator's psql, other applications.
DEFAULT_CONNECTION_RESERVE = 10


class ConnectionBudgetError(RuntimeError):
    """The processes asked for cannot fit in the connections PostgreSQL allows."""


@dataclass(frozen=True)
class ConnectionPlan:
    pools: dict[str, int]  # role -> max pool size of ONE process of that role
    processes: dict[str, int]  # role -> how many processes
    usable: int  # connections this application may use at all (server, role and database limits)
    reserve: int  # of those, kept free for non-ARC use

    @property
    def budget(self) -> int:
        return self.usable - self.reserve

    @property
    def total(self) -> int:
        return sum(self.processes.get(r, 0) * n for r, n in self.pools.items())

    def to_json(self) -> str:
        import json

        return json.dumps(
            {"pools": self.pools, "processes": self.processes, "usable": self.usable, "reserve": self.reserve},
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> "ConnectionPlan | None":
        """None for anything that is not a plan we wrote (a hand-edited setting must
        never stop a process from starting)."""
        import json

        try:
            raw = json.loads(text)
            return cls(
                pools={str(k): int(v) for k, v in raw["pools"].items()},
                processes={str(k): int(v) for k, v in raw["processes"].items()},
                usable=int(raw["usable"]),
                reserve=int(raw["reserve"]),
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return None


def plan_connection_pools(
    *,
    usable: int,
    reserve: int,
    processes: dict[str, int],
    desired: dict[str, int] | None = None,
    floors: dict[str, int] | None = None,
    fixed: dict[str, int] | None = None,
) -> ConnectionPlan:
    """Split `usable - reserve` connections over the processes.

    Every role gets its desired pool size if everything fits. If not, the roles that are
    not `fixed` are scaled down together (never below their floor), then any connections
    left over are handed back, gateway first. `fixed` pools are an operator's explicit
    choice for a role: they are kept as given and counted against the budget first. When
    even the floors do not fit it raises ConnectionBudgetError saying by how much."""
    desired = {**POOL_DESIRED, **(desired or {})}
    floors = {**POOL_FLOORS, **(floors or {})}
    fixed = fixed or {}
    roles = [r for r, n in processes.items() if n > 0]
    budget = usable - reserve
    if budget < 1:
        raise ConnectionBudgetError(
            f"PostgreSQL lets this application open {usable} connection(s) and {reserve} are reserved for "
            f"migrations, admin sessions and backups — nothing is left for the application."
        )

    fixed_cost = sum(processes[r] * fixed[r] for r in roles if r in fixed)
    free = [r for r in roles if r not in fixed]
    remaining = budget - fixed_cost
    floor_cost = sum(processes[r] * floors.get(r, 1) for r in free)
    if remaining < floor_cost:
        raise ConnectionBudgetError(
            f"{floor_cost + fixed_cost} connections are needed at the very least "
            f"({', '.join(f'{processes[r]} {r} x {fixed.get(r, floors.get(r, 1))}' for r in roles)}) "
            f"but only {budget} are available ({usable} allowed minus {reserve} reserved). Run fewer "
            f"workers, raise max_connections in PostgreSQL, or lower pgdb_connection_reserve."
        )

    want = {r: desired.get(r, 1) for r in free}
    sizes = dict(want)
    if sum(processes[r] * sizes[r] for r in free) > remaining:
        total_want = sum(processes[r] * want[r] for r in free)
        sizes = {r: max(floors.get(r, 1), int(want[r] * remaining / total_want)) for r in free}
        while sum(processes[r] * sizes[r] for r in free) > remaining:  # the floors can overshoot
            r = max((r for r in free if sizes[r] > floors.get(r, 1)), key=lambda r: sizes[r])
            sizes[r] -= 1
    # hand back what is left, most important role first, never past what was wanted
    grew = True
    while grew:
        grew = False
        for r in sorted(free, key=lambda r: (r != ROLE_GATEWAY, r)):
            if sizes[r] < want[r] and sum(processes[x] * sizes[x] for x in free) + processes[r] <= remaining:
                sizes[r] += 1
                grew = True
    return ConnectionPlan(pools={**{r: fixed[r] for r in roles if r in fixed}, **sizes}, processes={r: processes[r] for r in roles}, usable=usable, reserve=reserve)


def process_counts(gateway: int | None, queue_workers: dict[str, int] | int, scheduler: bool) -> dict[str, int]:
    """How many database-using processes of each kind a topology runs."""
    workers = queue_workers if isinstance(queue_workers, int) else sum(queue_workers.values())
    return {
        ROLE_GATEWAY: gateway or 0,
        ROLE_LINEUP_WORKER: workers,
        ROLE_LINEUP_SCHEDULER: 1 if scheduler else 0,
    }


#: The pool size a process gets when nothing sized it (`pgdb_pool_max_size`'s own default).
DEFAULT_POOL_SIZE = 10


def connection_problems(
    plan: "ConnectionPlan | None",
    processes: dict[str, int],
    usable_now: int | None,
    *,
    reserve: int = DEFAULT_CONNECTION_RESERVE,
    apply_command: str = "arc deploy",
) -> list[str]:
    """What is wrong between a deployment, the plan made for it and what PostgreSQL allows
    RIGHT NOW. Pure: the callers (`arc status`, `arc doctor --deep`) gather the facts.

    * no plan: every process opens the default-sized pool, which may be more than the server has;
    * a plan made for other process counts: someone changed a worker count and did not re-plan;
    * a plan larger than what the server allows today (its limit was lowered): the processes
      shrink themselves when they start, but the plan should be redone."""
    running = {r: n for r, n in processes.items() if n}
    if not running:
        return []
    problems: list[str] = []
    if plan is None:
        if usable_now is not None:
            want = sum(n * DEFAULT_POOL_SIZE for n in running.values())
            if want > usable_now - reserve:
                problems.append(
                    f"no connection plan: {sum(running.values())} processes would each open up to "
                    f"{DEFAULT_POOL_SIZE} connections ({want}) but PostgreSQL allows {usable_now} "
                    f"({reserve} kept free) — run `{apply_command}` to split them."
                )
        return problems
    planned = {r: n for r, n in plan.processes.items() if n}
    if planned != running:
        problems.append(
            "the connection plan was made for "
            + ", ".join(f"{n} {r}" for r, n in sorted(planned.items()))
            + " but this deployment runs "
            + ", ".join(f"{n} {r}" for r, n in sorted(running.items()))
            + f" — run `{apply_command}` to re-plan."
        )
    if usable_now is not None and plan.total > usable_now - plan.reserve:
        problems.append(
            f"the plan uses {plan.total} connections but PostgreSQL now allows {usable_now} "
            f"({plan.reserve} kept free) — each process shrinks its pool when it starts; run "
            f"`{apply_command}` to re-plan."
        )
    return problems


def fit_pool_to_server(plan: ConnectionPlan, role: str, usable_now: int, floors: dict[str, int] | None = None) -> int:
    """The pool size a process of `role` should really use, given what the server allows
    NOW. A deployment is planned once; PostgreSQL's limit can be lowered afterwards. Each
    process re-runs the same planner on the same inputs (the planned pools as the wishes,
    the same process counts), so they all arrive at the same answer and, together, are
    guaranteed to fit the new limit. Equal to the plan whenever the plan still fits; never
    larger than it. If even the floors cannot fit, the role's floor — a best effort the
    caller should report."""
    planned = plan.pools.get(role, 1)
    floor_map = {**POOL_FLOORS, **(floors or {})}
    if plan.total <= usable_now - plan.reserve or plan.total <= 0:
        return planned
    try:
        again = plan_connection_pools(
            usable=usable_now,
            reserve=plan.reserve,
            processes=plan.processes,
            desired=plan.pools,
            floors=floor_map,
        )
    except ConnectionBudgetError:
        return min(planned, floor_map.get(role, 1))
    return min(planned, again.pools.get(role, planned))
