"""
plugins.redix.cli
================
Three command groups contributed to Points.CLI_COMMANDS:

  arc cache      ping / stats / clear / get          (inspect the cache)
  arc queue      worker / status / dead / tasks       (run + inspect the queue)
  arc scheduler  worker / list / history / trigger     (run + inspect schedules)

Worker commands (``arc queue worker``, ``arc scheduler worker``) boot the shared
Arc lifecycle (which opens the Redis pool and registers @relay.task /
@relay.scheduled handlers via relay discovery), then run their loop until
interrupted. Inspection commands run a short bootstrap, do their read, tear down.

``arc schedule export`` is the system-cron fallback generator: it prints (or
writes) crontab lines for every registered schedule, each invoking
``arc schedule run <name>``.
"""

from __future__ import annotations

import asyncio
import signal

import typer

from arc.kernel.logger import get_logger
from arc.kernel.orchestrator import Arc

log = get_logger("arc.plugin.redix.cli")


def _bootstrap(coro):
    """Run *coro* inside a started kernel lifecycle, then tear down.

    Mirrors authn/psqldb CLI: build the shared Arc, start its lifecycle (opens
    the Redis pool and runs relay discovery so handlers are registered), run the
    coroutine, shut down.
    """
    async def _inner():
        app = Arc.shared()
        assert app.lifecycle is not None
        own = not app.lifecycle.started
        if own:
            await app.lifecycle.startup()
        try:
            return await coro
        finally:
            if own:
                await app.lifecycle.shutdown()

    return asyncio.run(_inner())


async def _run_worker(make_worker):
    """Start the lifecycle, build the worker, install signal handlers, run until
    SIGINT/SIGTERM, then shut down cleanly. ``make_worker`` is called after the
    lifecycle is up (so capabilities + handlers exist) and returns a worker with
    ``.run()`` / ``.stop()``."""
    app = Arc.shared()
    assert app.lifecycle is not None
    await app.lifecycle.startup()
    worker = make_worker(app)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    try:
        await worker.run()
    finally:
        await app.lifecycle.shutdown()


# ── caps lookup helpers ──────────────────────────────────────────────────────

def _cap(app: Arc, name: str):
    cap = app.capabilities.get(name)
    if cap is None:
        typer.secho(f"redix capability '{name}' is not available "
                    f"(is the redix plugin installed?).",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    return cap


# ── cache group ──────────────────────────────────────────────────────────────

def _build_cache_cli() -> typer.Typer:
    app_cli = typer.Typer(name="cache", help="Inspect the redix cache.",
                          no_args_is_help=True)

    @app_cli.command()
    def ping() -> None:
        """Check Redis connectivity for the cache."""
        async def _do():
            ok = await _cap(Arc.shared(), "cache.client").ping()
            typer.echo("PONG" if ok else "no response")
        _bootstrap(_do())

    @app_cli.command()
    def get(key: str = typer.Argument(..., help="Cache key (without prefix).")) -> None:
        """Print a single cached value."""
        async def _do():
            val = await _cap(Arc.shared(), "cache.client").get(key)
            typer.echo(repr(val))
        _bootstrap(_do())

    @app_cli.command()
    def clear(prefix: str = typer.Option("", "--prefix",
                                         help="Only clear keys under this prefix.")) -> None:
        """Delete cached keys (optionally only those under --prefix)."""
        async def _do():
            client = _cap(Arc.shared(), "cache.client")
            removed = await client.delete_prefix(prefix)
            typer.echo(f"Removed {removed} key(s).")
        _bootstrap(_do())

    return app_cli


# ── queue group ──────────────────────────────────────────────────────────────

def _build_queue_cli() -> typer.Typer:
    app_cli = typer.Typer(name="queue", help="Run and inspect the redix queue.",
                          no_args_is_help=True)

    @app_cli.command()
    def worker(
        queue: list[str] = typer.Option(None, "--queue",
                                        help="Priority stream(s) to consume "
                                             "(high/default/low). Repeatable."),
        concurrency: int = typer.Option(1, "--concurrency",
                                        help="Concurrent task slots."),
    ) -> None:
        """Start a queue worker process (consumes jobs until interrupted)."""
        from plugins.redix.queue.worker import QueueWorker
        from plugins.relay import relay as registrar

        def make(app: Arc) -> QueueWorker:
            q = _cap(app, "queue.client")
            return QueueWorker(q, registrar, queues=queue or None,
                               concurrency=concurrency)

        asyncio.run(_run_worker(make))

    @app_cli.command()
    def status() -> None:
        """Show pending / dead counts per stream."""
        async def _do():
            st = await _cap(Arc.shared(), "queue.client").status()
            typer.echo(f"dead: {st['dead']}")
            for prio, n in st["streams"].items():
                typer.echo(f"  {prio:<8} {n} pending")
        _bootstrap(_do())

    dead_app = typer.Typer(name="dead", help="Dead-letter inspection.",
                           no_args_is_help=True)

    @dead_app.command("list")
    def dead_list(limit: int = typer.Option(50, "--limit")) -> None:
        """List dead-letter jobs."""
        async def _do():
            items = await _cap(Arc.shared(), "queue.client").dead_list(limit=limit)
            if not items:
                typer.echo("No dead jobs.")
                return
            for j in items:
                typer.echo(f"  {j.get('id')}  {j.get('task')}  "
                           f"attempts={j.get('attempts')}  error={j.get('error')}")
        _bootstrap(_do())

    @dead_app.command("retry")
    def dead_retry(job_id: str = typer.Argument(...)) -> None:
        """Requeue one dead job by id."""
        async def _do():
            ok = await _cap(Arc.shared(), "queue.client").dead_retry(job_id)
            typer.echo("requeued" if ok else "not found")
        _bootstrap(_do())

    @dead_app.command("purge")
    def dead_purge() -> None:
        """Clear the dead-letter queue."""
        async def _do():
            n = await _cap(Arc.shared(), "queue.client").dead_purge()
            typer.echo(f"Purged {n} dead job(s).")
        _bootstrap(_do())

    app_cli.add_typer(dead_app)

    @app_cli.command()
    def tasks() -> None:
        """List every @relay.task-registered task name."""
        def _do():
            from plugins.relay import relay as registrar
            names = registrar.task_names()
            if not names:
                typer.echo("No tasks registered.")
                return
            for n in names:
                typer.echo(f"  {n}")
        # No Redis needed — just needs handlers imported (the build did that).
        Arc.shared()
        _do()

    return app_cli


# ── scheduler group ──────────────────────────────────────────────────────────

def _build_scheduler_cli() -> typer.Typer:
    app_cli = typer.Typer(name="scheduler", help="Run and inspect schedules.",
                          no_args_is_help=True)

    @app_cli.command()
    def worker(
        id: str = typer.Option(..., "--id",
                               help="This instance's id; must match the "
                                    "configured leader_id to actually tick."),
    ) -> None:
        """Start the scheduler clock (only ticks if --id matches leader_id)."""
        from plugins.redix.scheduler.worker import SchedulerWorker

        def make(app: Arc) -> SchedulerWorker:
            sched = _cap(app, "scheduler.client")
            q = _cap(app, "queue.client")
            return SchedulerWorker(sched, q, instance_id=id)

        asyncio.run(_run_worker(make))

    @app_cli.command("list")
    def list_cmd() -> None:
        """List registered schedules with their next-run hint."""
        async def _do():
            specs = await _cap(Arc.shared(), "scheduler.client").list()
            if not specs:
                typer.echo("No schedules registered.")
                return
            for s in specs:
                when = s.get("expr") or f"every {s.get('seconds')}s"
                typer.echo(f"  {s.get('name'):<28} {s.get('kind'):<6} {when}")
        _bootstrap(_do())

    @app_cli.command()
    def history(name: str = typer.Argument(...),
                limit: int = typer.Option(20, "--limit")) -> None:
        """Show recent dispatch history for one schedule."""
        async def _do():
            rows = await _cap(Arc.shared(), "scheduler.client").history(name, limit=limit)
            if not rows:
                typer.echo("No history.")
                return
            for r in rows:
                typer.echo(f"  dispatched_at={r.get('dispatched_at')}  "
                           f"job_id={r.get('job_id')}")
        _bootstrap(_do())

    @app_cli.command()
    def trigger(name: str = typer.Argument(...)) -> None:
        """Manually fire a schedule now (enqueues immediately)."""
        async def _do():
            q = _cap(Arc.shared(), "queue.client")
            job_id = await q.enqueue(name)
            await _cap(Arc.shared(), "scheduler.client").record_run(name, job_id=job_id)
            typer.echo(f"Triggered {name} → job {job_id}")
        _bootstrap(_do())

    return app_cli


# ── schedule export (system-cron fallback) + run (one-shot) ──────────────────

def _build_schedule_cli() -> typer.Typer:
    app_cli = typer.Typer(name="schedule",
                          help="System-cron fallback: export crontab + run one-shot.",
                          no_args_is_help=True)

    @app_cli.command()
    def export(
        write: str = typer.Option(None, "--write",
                                  help="Write crontab lines to this path "
                                       "(e.g. /etc/cron.d/arc) instead of stdout."),
        with_flock: bool = typer.Option(False, "--with-flock",
                                        help="Wrap each line in flock for overlap "
                                             "protection."),
    ) -> None:
        """Generate system crontab lines for every registered schedule.

        Each cron entry invokes ``arc schedule run <name>``. Arc never installs
        these itself — review and place them deliberately.
        """
        async def _collect():
            specs = await _cap(Arc.shared(), "scheduler.client").list()
            return specs

        specs = _bootstrap(_collect())
        lines: list[str] = []
        for s in specs:
            name = s.get("name")
            if s.get("kind") == "cron":
                expr = s.get("expr", "")
            else:
                # 'every N seconds' has no exact crontab equivalent; emit a
                # minute-granular approximation and note it.
                secs = int(s.get("seconds", 0) or 0)
                minutes = max(1, secs // 60)
                expr = f"*/{minutes} * * * *"
            cmd = f"arc schedule run {name}"
            if with_flock:
                cmd = f"flock -n /tmp/arc-sched-{name}.lock {cmd}"
            lines.append(f"{expr} {cmd}")

        body = "\n".join(lines) + ("\n" if lines else "")
        if write:
            from pathlib import Path
            Path(write).write_text(body, encoding="utf-8")
            typer.echo(f"Wrote {len(lines)} cron line(s) to {write}")
        else:
            typer.echo(body or "# no schedules registered")

    @app_cli.command()
    def run(name: str = typer.Argument(..., help="Schedule/task name to run once.")) -> None:
        """Run a single scheduled task once, directly, then exit.

        This is the command system cron invokes. It runs the handler inline
        (not via the queue) since cron itself is the scheduler in fallback mode.
        """
        async def _do():
            from plugins.relay import relay as registrar
            handler = registrar.task_handler(name) or registrar.scheduled_handler(name)
            if handler is None:
                typer.secho(f"No task/schedule handler named {name!r}.",
                            fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
            result = handler()
            if asyncio.iscoroutine(result):
                await result
            typer.echo(f"ran {name}")
        _bootstrap(_do())

    return app_cli


# ── aggregate ────────────────────────────────────────────────────────────────

def build_cli() -> list:
    """redix contributes FOUR Typer groups. The kernel's mounter accepts a
    Typer or (here) a list of them — see plugin.contribute()."""
    return [
        _build_cache_cli(),
        _build_queue_cli(),
        _build_scheduler_cli(),
        _build_schedule_cli(),
    ]


__all__ = ["build_cli"]