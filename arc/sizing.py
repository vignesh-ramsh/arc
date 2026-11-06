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
