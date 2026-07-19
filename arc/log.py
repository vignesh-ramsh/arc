"""
arc.log
------------------
Kernel service module (same tier as arc.codec/arc.events/arc.health, §3.10/
§3.18) — configures Python's stdlib root logger ONCE, at the very start of
arc.boot(), before any plugin's register() runs. Every plugin already does
`logging.getLogger(name)` (gateway, authn, lineup, events) — a logger with
no handlers of its own propagates to root, so configuring root here is what
every existing call site benefits from, with zero changes to any of them.

Two things are always on: a console handler (human-readable, keeps `arc
run`'s live-tailing exactly as it already works) and a CATEGORY router that
sends each log line to one of a handful of rotating JSON-lines files under
<project_root>/logs/ (already created by `arc init`'s scaffolding, already
gitignored) — grouped by what part of the system produced it, not by which
OS process: `system.jsonl` (the kernel itself, plus any plugin not called
out below — authn, admin, mail, hrms, redix), `gateway.jsonl`, `relay.jsonl`,
`db.jsonl` (psqldb), `queue.jsonl` (lineup workers), `scheduler.jsonl`
(lineup's scheduler process specifically). lineup uses the same logger
names ("lineup", "lineup.cli") for both roles, so distinguishing queue from
scheduler needs to know which process this is, not just the logger name —
see set_role() below.

A known, accepted trade-off, not an oversight: stdlib's RotatingFileHandler
isn't safe for multiple processes rotating the SAME file concurrently (each
tracks its own size counter independently), and `arc run` can spawn N
gateway workers all sharing gateway.jsonl. Every line still carries its own
`pid` field, so a specific worker's lines are always findable even sharing
a file — kept this way deliberately (asked for as "basic logging", one file
per category to actually look at, not one per process to grep across); if
rotation races under real multi-worker load ever become a real problem, the
fix is external rotation (logrotate) rather than more in-process machinery.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_LEVEL_KEY = "log_level"
LOG_MAX_BYTES_KEY = "log_max_bytes"
LOG_BACKUP_COUNT_KEY = "log_backup_count"

DEFAULT_LEVEL = "INFO"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per category file before rotating
DEFAULT_BACKUP_COUNT = 5

_CONSOLE_FORMAT = "[%(asctime)s] %(name)s: %(message)s"

# Logger-name prefix -> category file stem. Checked against the record's
# own logger name ("gateway", "gateway.middleware", ...) — exact match or
# a "<prefix>." start. Anything not matched here (authn, admin, mail,
# hrms, redix, the arc kernel's own loggers, ...) falls into "system", the
# catch-all the user actually asked for ("all arc.system/plugins related
# logs" together).
_CATEGORY_PREFIXES = {
    "gateway": "gateway",
    "relay": "relay",
    "psqldb": "db",
}

# The standard attributes every LogRecord already carries — anything ELSE
# on a record came from a caller's own `extra={...}` kwarg and belongs in
# the JSON output as a real structured field, not just folded into the
# message string. "asctime"/"message" aren't part of a fresh LogRecord's
# own __dict__ (below) — logging.Formatter.format() adds them as a SIDE
# EFFECT on the shared record object the first time any handler's
# formatter runs, console or file, whichever happens to go first — so a
# real record built by this project's own console handler running before
# the JSON one leaked a spurious "asctime" field until these two were
# added here explicitly, found by checking actual JSON output, not by
# reading logging's docs and assuming it was fine.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime", "message",
}

# This process's own role, if it has one — set by set_role() at the exact
# moment gateway/lineup's own process-bridge wiring already learns it
# (§3.18/§3.19). Only actually consulted for records logged under the
# "lineup" name, to decide queue vs scheduler; every other category is
# fully determined by the logger name alone, regardless of role.
_current_role: str | None = None
_router: "_CategoryRouter | None" = None


class JsonFormatter(logging.Formatter):
    """One JSON object per line. `relay_level` (relay.log()'s own
    info/success/warning/error vocabulary, §3.11) is surfaced as `level`
    directly when a record carries it, instead of collapsing "success"
    into a plain "INFO" that would lose the distinction relay.log()
    callers actually asked for."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": getattr(record, "relay_level", record.levelname.lower()),
            "logger": record.name,
            "pid": record.process,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key == "relay_level":
                continue
            entry[key] = value
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def category_for(logger_name: str) -> str:
    """Which logs/<category>.jsonl a record from this logger name belongs
    in. "lineup" (or "lineup.<anything>") is the one name whose category
    depends on this PROCESS's own role rather than the name alone — a
    worker and a scheduler process both log under the same name, only the
    role tells them apart."""
    if logger_name == "lineup" or logger_name.startswith("lineup."):
        return "scheduler" if _current_role == "lineup-scheduler" else "queue"
    for prefix, category in _CATEGORY_PREFIXES.items():
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            return category
    return "system"


class _CategoryRouter(logging.Handler):
    """One handler attached to root; internally fans out to one rotating
    JSON file per category, created lazily on first use — a plain CLI
    command that only ever logs to "system" never creates gateway.jsonl/
    relay.jsonl/etc. it will never write to."""

    def __init__(self, logs_dir: Path, *, max_bytes: int, backup_count: int) -> None:
        super().__init__()
        self._logs_dir = logs_dir
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._handlers: dict[str, logging.handlers.RotatingFileHandler] = {}

    def _handler_for(self, category: str) -> logging.handlers.RotatingFileHandler:
        handler = self._handlers.get(category)
        if handler is None:
            handler = logging.handlers.RotatingFileHandler(
                self._logs_dir / f"{category}.jsonl",
                maxBytes=self._max_bytes, backupCount=self._backup_count,
            )
            handler.setFormatter(JsonFormatter())
            self._handlers[category] = handler
        return handler

    def emit(self, record: logging.LogRecord) -> None:
        self._handler_for(category_for(record.name)).emit(record)

    def close(self) -> None:
        for handler in self._handlers.values():
            handler.close()
        super().close()


def configure(kernel: Any) -> None:
    """Called once per process, from arc.runtime.boot() — before any
    plugin's register() runs, so a plugin registration issue (kernel.advise,
    a genuine bug) already logs through this. Idempotent within a process:
    clears any handlers a prior configure() (or, in lineup's case, an
    earlier bare logging.basicConfig() call this replaces) already
    attached, so a second boot() in the same process (tests, `force=True`)
    never double-attaches handlers and double-prints every line."""
    global _router

    settings = kernel.settings
    level_name = DEFAULT_LEVEL
    max_bytes = DEFAULT_MAX_BYTES
    backup_count = DEFAULT_BACKUP_COUNT
    if settings is not None:
        settings.declare(LOG_LEVEL_KEY)
        settings.declare(LOG_MAX_BYTES_KEY)
        settings.declare(LOG_BACKUP_COUNT_KEY)
        level_name = settings.get(LOG_LEVEL_KEY) or DEFAULT_LEVEL
        max_bytes = int(settings.get(LOG_MAX_BYTES_KEY) or DEFAULT_MAX_BYTES)
        backup_count = int(settings.get(LOG_BACKUP_COUNT_KEY) or DEFAULT_BACKUP_COUNT)

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level_name)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    root.addHandler(console)

    logs_dir = (kernel.project_root or Path.cwd()) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    _router = _CategoryRouter(logs_dir, max_bytes=max_bytes, backup_count=backup_count)
    root.addHandler(_router)


def set_role(role: str) -> None:
    """Records this process's own role — gateway's ASGI lifespan startup,
    lineup worker/scheduler boot, the exact same moment each already calls
    arc.events.install_process_bridge (§3.18) — so category_for() can tell
    a lineup worker's logs (queue.jsonl) apart from a lineup scheduler's
    (scheduler.jsonl) despite both logging under the same logger name. A
    plain CLI command never calls this, and doesn't need to — nothing it
    logs depends on a role to categorize correctly."""
    global _current_role
    _current_role = role
