"""
arc.errorscmd
-----------------
`arc errors` - what has been failing: the lines arc.errors wrote to logs/errors.jsonl, grouped by
fingerprint, so a thousand occurrences of one bug read as one row with a count rather than a thousand
tracebacks. This is the error tracker you have without a Sentry account; with one, the same events
are there too (see arc.errors).

    arc errors                      # the last 24 hours, worst first
    arc errors --hours 1 --check    # exit 1 if anything failed in the last hour (a cron/monitoring check)
    arc errors --source lineup --json

Named errorscmd.py, like healthcmd.py and backupcmd.py, so it never collides with the arc.errors module.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from .runtime import find_project_root

console = Console()
err_console = Console(stderr=True, style="bold red")


@dataclass
class Problem:
    """Every occurrence of one fingerprint inside the window."""

    fingerprint: str
    error_type: str
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    message: str = ""
    sources: Counter = field(default_factory=Counter)
    where: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "error_type": self.error_type,
            "count": self.count,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "message": self.message,
            "sources": dict(self.sources),
            "where": self.where,
            "context": self.context,
        }


def log_files(logs_dir: Path) -> list[Path]:
    """The errors log and everything that belongs with it: rotated copies (`errors.jsonl.1`...) and
    the per-process files a process writes when the collector is not reachable."""
    found = [*logs_dir.glob("errors.jsonl"), *logs_dir.glob("errors.jsonl.*"), *logs_dir.glob("errors.fallback-*.jsonl")]
    return sorted(set(found))


def _read(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a torn last line from a crash; the rest of the file is fine
                if isinstance(entry, dict) and entry.get("fingerprint"):
                    entries.append(entry)
    except OSError:
        pass
    return entries


def _when(entry: dict[str, Any]) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(entry["timestamp"]))
    except (KeyError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _message(entry: dict[str, Any]) -> str:
    """The exception's message: its own field when the writer gave one, else the log line's text
    without the `Type: ` it starts with."""
    if "error_message" in entry:
        return str(entry["error_message"])
    text = str(entry.get("message", ""))
    prefix = f"{entry.get('error_type', '')}: "
    return text[len(prefix) :] if prefix != ": " and text.startswith(prefix) else text


def _where(entry: dict[str, Any]) -> str:
    """What to look at: the route for a request, the job (and queue) for a background job, else the
    innermost frame."""
    context = entry.get("error_context") or {}
    if context.get("route") and context["route"] != "-":
        return f"{context.get('method', '')} {context['route']}".strip()
    if context.get("job"):
        return f"job {context['job']}" + (f" ({context['queue']})" if context.get("queue") else "")
    if context.get("stage"):
        return f"{entry.get('source', '?')} {context['stage']}"
    frames = entry.get("frames") or []
    return str(frames[-1]) if frames else "-"


def summarise(
    logs_dir: Path, *, since: datetime, source: str | None = None, now: datetime | None = None
) -> list[Problem]:
    """The problems seen since `since`, most frequent first (ties: most recent first)."""
    problems: dict[str, Problem] = {}
    for path in log_files(logs_dir):
        for entry in _read(path):
            when = _when(entry)
            if when is None or when < since or (now is not None and when > now):
                continue
            if source and entry.get("source") != source:
                continue
            problem = problems.setdefault(
                entry["fingerprint"], Problem(entry["fingerprint"], str(entry.get("error_type", "?")))
            )
            problem.count += 1
            problem.sources[str(entry.get("source", "?"))] += 1
            if problem.first_seen is None or when < problem.first_seen:
                problem.first_seen = when
            if problem.last_seen is None or when >= problem.last_seen:
                problem.last_seen = when
                problem.message = _message(entry)
                problem.where = _where(entry)
                problem.context = entry.get("error_context") or {}
    return sorted(
        problems.values(),
        key=lambda p: (-p.count, -(p.last_seen.timestamp() if p.last_seen else 0)),
    )


def errors_command(
    hours: float = typer.Option(24.0, "--hours", min=0.01, help="How far back to look."),
    source: str = typer.Option(None, "--source", help="Only errors from this source (gateway, lineup, ...)."),
    limit: int = typer.Option(20, "--limit", min=1, help="Show at most this many distinct problems."),
    as_json: bool = typer.Option(False, "--json", help="Print the problems as JSON (all of them, not just --limit)."),
    check: bool = typer.Option(False, "--check", help="Exit 1 if there is any error in the window, for a monitoring check."),
) -> None:
    """What has been failing, grouped by bug: how many times, when it last happened and where.
    Reads logs/errors.jsonl, which every gateway, worker and scheduler process writes through
    arc.errors; counts are exact only for as much history as the log keeps (log_max_bytes,
    log_backup_count)."""
    root = find_project_root()
    if root is None:
        err_console.print("Not an ARC project (no .arc/arc.toml found here or in any parent).")
        raise typer.Exit(code=1)
    now = datetime.now(UTC)
    problems = summarise(root / "logs", since=now - timedelta(hours=hours), source=source, now=now)
    total = sum(p.count for p in problems)

    if as_json:
        typer.echo(json.dumps({"hours": hours, "errors": total, "problems": [p.to_json() for p in problems]}))
    elif not problems:
        console.print(f"[green]No errors recorded in the last {hours:g} hours[/green]" + (f" from {source}" if source else "") + ".")
    else:
        console.print(f"{total} error(s), {len(problems)} distinct problem(s) in the last {hours:g} h", highlight=False)
        for problem in problems[:limit]:
            seen = problem.last_seen.strftime("%m-%d %H:%M:%S") if problem.last_seen else "-"
            console.print(
                f"\n[bold]{problem.count:>5} ×[/bold]  {escape(problem.where)}  "
                f"[dim]({escape(','.join(sorted(problem.sources)))})  last {seen} UTC  id {problem.fingerprint}[/dim]\n"
                f"         {escape(f'{problem.error_type}: {problem.message}'[:300])}",
                soft_wrap=True,
                highlight=False,
            )
        if len(problems) > limit:
            console.print(f"\n[dim]{len(problems) - limit} more; raise --limit or use --json[/dim]")
    if check and problems:
        raise typer.Exit(code=1)
