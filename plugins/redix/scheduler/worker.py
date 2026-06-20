"""
plugins.redix.scheduler.worker
=============================
The scheduler clock. Runs as a separate ``arc scheduler worker --id <id>``
process. Only the process whose ``--id`` matches the configured ``leader_id``
actually ticks and dispatches; any other instance idles passively (logs a
warning and does nothing) so a second replica can never double-fire crons.

Each tick:
  1. read all registered schedules from Redis;
  2. for each schedule that is DUE, acquire a short-lived overlap lock
     (``SET NX EX``) keyed by job name — if held, skip this run;
  3. dispatch the job onto the queue (``queue.enqueue``) and record history.

Dispatch-onto-queue (not inline) means scheduled jobs inherit retry/backoff/
dead-letter from the queue worker, and a slow job can't delay the next tick.

Cron parsing uses ``croniter`` if available; for ``every`` schedules a simple
last-run timestamp comparison is used (no external dep needed).
"""

from __future__ import annotations

import asyncio
import time

from arc.kernel.logger import get_logger
from plugins.redix.scheduler.client import SchedulerClient

log = get_logger("arc.plugin.redix.scheduler.worker")

_TICK_SECONDS = 5.0
_LOCK_TTL = 30  # seconds an overlap lock is held before auto-expiry


def _cron_is_due(expr: str, now: float, last: float | None) -> bool:
    """True if a cron expr is due between *last* and *now*. Uses croniter when
    available; otherwise falls back to 'never' (logs once) so a missing dep is
    visible rather than silently mis-firing."""
    try:
        from croniter import croniter
    except ImportError:  # pragma: no cover
        log.warning("arc.redix.croniter_missing",
                    detail="install croniter for cron schedules; this schedule "
                           "will not fire", expr=expr)
        return False
    base = last if last is not None else now - _TICK_SECONDS
    itr = croniter(expr, base)
    nxt = itr.get_next(float)
    return nxt <= now


class SchedulerWorker:
    def __init__(self, scheduler: SchedulerClient, queue, *,
                 instance_id: str) -> None:
        self._sched = scheduler
        self._queue = queue
        self._id = instance_id
        self._stop = asyncio.Event()
        self._last_run: dict[str, float] = {}

    @property
    def is_leader(self) -> bool:
        return self._id == self._sched.leader_id

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self.is_leader:
            log.warning(
                "arc.redix.scheduler_not_leader",
                detail=("this instance id does not match the configured "
                        "leader_id; scheduler will idle and NOT dispatch jobs"),
                instance_id=self._id, leader_id=self._sched.leader_id,
            )
            # Idle passively so the process can still be supervised/health-checked.
            await self._stop.wait()
            return

        log.info("arc.redix.scheduler_leader", instance_id=self._id)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                log.error("arc.redix.scheduler_tick_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
        log.info("arc.redix.scheduler_stopped", instance_id=self._id)

    async def _tick(self) -> None:
        now = time.time()
        schedules = await self._sched.list()
        for spec in schedules:
            name = spec.get("name")
            if not name:
                continue
            if not self._due(spec, now):
                continue
            # Overlap protection: only one run per job name at a time.
            got = await self._sched._conn.client.set(
                self._sched._lock_key(name), b"1", nx=True, ex=_LOCK_TTL
            )
            if not got:
                log.info("arc.redix.schedule_skipped_locked", name=name)
                continue
            await self._dispatch(name)
            self._last_run[name] = now

    def _due(self, spec: dict, now: float) -> bool:
        name = spec.get("name", "")
        last = self._last_run.get(name)
        kind = spec.get("kind")
        if kind == "every":
            secs = float(spec.get("seconds", 0) or 0)
            if secs <= 0:
                return False
            return last is None or (now - last) >= secs
        if kind == "cron":
            return _cron_is_due(spec.get("expr", ""), now, last)
        return False

    async def _dispatch(self, name: str) -> None:
        """Enqueue the scheduled job's task onto the queue and record history.

        The scheduled handler is registered under the schedule name via
        ``@relay.scheduled(name)``; the queue worker resolves it the same way it
        resolves any ``@relay.task``. We enqueue using the schedule name as the
        task name."""
        try:
            job_id = await self._queue.enqueue(name)
        except Exception as exc:  # noqa: BLE001
            log.error("arc.redix.schedule_dispatch_error", name=name, error=str(exc))
            return
        await self._sched.record_run(name, job_id=job_id)
        log.info("arc.redix.schedule_dispatched", name=name, job_id=job_id)