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
run`'s live-tailing as it already works, except for loggers a plugin has
marked file-only via file_only() below) and a CATEGORY router that
sends each log line to one of a handful of rotating JSON-lines files under
<project_root>/logs/ (already created by `arc init`'s scaffolding, already
gitignored) — grouped by what part of the system produced it, not by which
OS process: `system.jsonl` (the kernel itself, plus any plugin not called
out below — authn, admin, mail, hrms, redix), `gateway.jsonl`, `relay.jsonl`,
`db.jsonl` (pgdb), `queue.jsonl` (lineup workers), `scheduler.jsonl`
(lineup's scheduler process specifically), and `errors.jsonl` (one line per
error reported through arc.errors.capture, whichever process hit it — see
arc.errors). lineup uses the same logger
names ("lineup", "lineup.cli") for both roles, so distinguishing queue from
scheduler needs to know which process this is, not just the logger name —
see set_role() below.

Who writes those files. Under `arc run`, ONE process does: arc.log_collector's
LogCollector (started by `arc run`, socket path in ARC_LOG_SOCKET). Every
other process — each gateway worker, lineup worker and scheduler, and `arc
run` itself — uses _CollectorHandler: a log call only puts the record on a
small in-process queue; a sender thread formats it and, every few
milliseconds, sends the accumulated lines to the collector, which appends
each category's lines with one write() and owns rotation. That removes the
old design's two problems: stdlib's RotatingFileHandler is not safe for
several processes rotating one file (each decides on its own that the file is
full; measured: only 67% of lines survived ~8,750 requests/s), and every log
line cost a synchronous write()+flush() on the request path.

Without a collector (a plain CLI command, a gateway started on its own, a
test) a process uses _CategoryRouter, which writes the files directly as it
always did. If the collector goes away while a process is running, that
process keeps logging to files of its own (`<category>.fallback-<pid>.jsonl`):
never to the shared files, which only the collector may write or rotate. Every line still carries
its own `pid` field, so a specific worker's lines are always findable.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import socket
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_LEVEL_KEY = "log_level"
LOG_MAX_BYTES_KEY = "log_max_bytes"
LOG_BACKUP_COUNT_KEY = "log_backup_count"
LOG_COLLECTOR_KEY = "log_collector"
LOG_FLUSH_MS_KEY = "log_flush_ms"

#: Set by `arc run` for everything it spawns: path of the collector's socket.
COLLECTOR_ENV = "ARC_LOG_SOCKET"
DEFAULT_FLUSH_MS = 10
#: Records a process may hold waiting for its sender thread (~37 MB at 370 B/line).
COLLECTOR_QUEUE_SIZE = 100_000
_SEND_BATCH = 4096
#: Format this many records, then yield the GIL, so a big batch never holds the
#: event-loop thread off the interpreter for a whole switch interval.
_FORMAT_CHUNK = 64
_DROP_REPORT_SECONDS = 10.0

DEFAULT_LEVEL = "INFO"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per category file before rotating
DEFAULT_BACKUP_COUNT = 5

_CONSOLE_FORMAT = "[%(asctime)s] %(name)s: %(message)s"
_CONSOLE_DATEFMT = "%H:%M:%S"

# Logger-name prefix -> category file stem. Checked against the record's
# own logger name ("gateway", "gateway.middleware", ...) — exact match or
# a "<prefix>." start. Anything not matched here (authn, admin, mail,
# hrms, redix, the arc kernel's own loggers, ...) falls into "system", the
# catch-all the user actually asked for ("all arc.system/plugins related
# logs" together).
_CATEGORY_PREFIXES = {
    "gateway": "gateway",
    "relay": "relay",
    "pgdb": "db",
    "arc.errors": "errors",  # arc.errors.capture(): one line per reported error, with a fingerprint
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
    "asctime",
    "message",
}

# This process's own role, if it has one — set by set_role() at the exact
# moment gateway/lineup's own process-bridge wiring already learns it
# (§3.18/§3.19). Only actually consulted for records logged under the
# "lineup" name, to decide queue vs scheduler; every other category is
# fully determined by the logger name alone, regardless of role.
_current_role: str | None = None
_router: "_CategoryRouter | _CollectorHandler | None" = None
# (logs_dir, SinkSettings) from the last configure(); what use_collector/use_direct rebuild from.
_sink: "tuple[Path, SinkSettings] | None" = None


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

    def __init__(
        self, logs_dir: Path, *, max_bytes: int, backup_count: int, name_suffix: str = ""
    ) -> None:
        super().__init__()
        self._logs_dir = logs_dir
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._name_suffix = name_suffix
        self._handlers: dict[str, logging.handlers.RotatingFileHandler] = {}

    def _handler_for(self, category: str) -> logging.handlers.RotatingFileHandler:
        handler = self._handlers.get(category)
        if handler is None:
            handler = logging.handlers.RotatingFileHandler(
                self._logs_dir / f"{category}{self._name_suffix}.jsonl",
                maxBytes=self._max_bytes,
                backupCount=self._backup_count,
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


@dataclass(frozen=True)
class SinkSettings:
    """The log settings every process (and `arc run`, which starts the
    collector) reads the same way."""

    level: str
    max_bytes: int
    backup_count: int
    collector: bool
    flush_seconds: float


def read_sink_settings(settings: Any) -> SinkSettings:
    level_name, max_bytes, backup_count = DEFAULT_LEVEL, DEFAULT_MAX_BYTES, DEFAULT_BACKUP_COUNT
    collector, flush_ms = True, DEFAULT_FLUSH_MS
    if settings is not None:
        settings.declare(
            LOG_LEVEL_KEY,
            type=str,
            default=DEFAULT_LEVEL,
            doc="How much detail goes into the logs: `DEBUG` (most), `INFO` (the default), `WARNING`, `ERROR` or "
            "`CRITICAL` (least).",
        )
        settings.declare(
            LOG_MAX_BYTES_KEY,
            type=int,
            default=DEFAULT_MAX_BYTES,
            doc="How big each log file may grow, in bytes, before it is rotated (default 10 MB).",
        )
        settings.declare(
            LOG_BACKUP_COUNT_KEY,
            type=int,
            default=DEFAULT_BACKUP_COUNT,
            doc="How many old copies of each log file are kept after it is rotated. The default is 5. Older ones "
            "are deleted.",
        )
        settings.declare(
            LOG_COLLECTOR_KEY,
            type=bool,
            default=True,
            doc="When on (the default), `arc run` starts one log collector that is the only program writing the "
            "`logs/*.jsonl` files, which avoids clashes between workers. When off, every process writes the "
            "log files itself.",
        )
        settings.declare(
            LOG_FLUSH_MS_KEY,
            type=int,
            default=DEFAULT_FLUSH_MS,
            doc="How many milliseconds each process gathers log lines before sending them to the collector, and "
            "how long the collector gathers them before writing them out in one go. A bigger number means "
            "fewer writes but logs appear slightly later.",
        )
        level_name = settings.get(LOG_LEVEL_KEY) or DEFAULT_LEVEL
        max_bytes = int(settings.get(LOG_MAX_BYTES_KEY) or DEFAULT_MAX_BYTES)
        backup_count = int(settings.get(LOG_BACKUP_COUNT_KEY) or DEFAULT_BACKUP_COUNT)
        collector = bool(settings.get(LOG_COLLECTOR_KEY))
        flush_ms = int(settings.get(LOG_FLUSH_MS_KEY) or DEFAULT_FLUSH_MS)
    return SinkSettings(level_name, max_bytes, backup_count, collector, flush_ms / 1000.0)


_live_collector_handlers: "weakref.WeakSet[_CollectorHandler]" = weakref.WeakSet()


class _CollectorHandler(logging.Handler):
    """Sends records to the log collector without ever making the caller wait.

    emit() — the only part that runs on a request — resolves the message and
    does queue.put_nowait. A sender thread does the rest: waits `send_interval`
    so records accumulate, formats them (chunked, yielding the GIL), and sends
    each category's lines over its own connection to the collector in one
    sendall. If the queue is full the record is dropped and counted (never
    blocks). If the collector is unreachable, or stalls past `send_timeout`, the
    sender writes the same records to the files directly through the fallback
    router, so nothing is lost; it retries the collector after `reconnect_backoff`.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        fallback_factory: Any,
        send_interval: float = DEFAULT_FLUSH_MS / 1000.0,
        queue_size: int = COLLECTOR_QUEUE_SIZE,
        send_timeout: float = 5.0,
        reconnect_backoff: float = 2.0,
    ) -> None:
        super().__init__()
        self._socket_path = socket_path
        self._fallback_factory = fallback_factory
        self._interval = send_interval
        self._queue_size = queue_size
        self._send_timeout = send_timeout
        self._backoff = reconnect_backoff
        self._formatter = JsonFormatter()
        self._conns: dict[str, socket.socket] = {}
        self._fallback: Any = None
        self._down_until = 0.0
        self._closed = False
        self._last_drop_report = 0.0
        self._reported_dropped = 0
        self.dropped = 0
        self.sent = 0
        self.fallback_lines = 0
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._thread = self._start_thread()
        _live_collector_handlers.add(self)

    # -------------------------------------------------------------- request path
    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Resolve the message now: args may be mutated or unprintable later.
            record.msg = record.getMessage()
            record.args = None
            self._queue.put_nowait((category_for(record.name), record))
        except queue.Full:
            self.dropped += 1
        except Exception:  # noqa: BLE001 - logging must never raise into the caller
            self.handleError(record)

    # -------------------------------------------------------------- sender thread
    def _start_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, name="arc-log-sender", daemon=True)
        thread.start()
        return thread

    def _run(self) -> None:
        q = self._queue  # captured: a fork replaces self._queue for the child
        while True:
            try:
                item = q.get(timeout=0.25)
            except queue.Empty:
                if self._closed:
                    return
                continue
            if self._interval and not self._closed:
                time.sleep(self._interval)  # let the queue fill; callers never wait on this
            batch = [item]
            while len(batch) < _SEND_BATCH:
                try:
                    batch.append(q.get_nowait())
                except queue.Empty:
                    break
            try:
                self._deliver(batch)
            except Exception as exc:  # noqa: BLE001 - the sender must outlive any one bad batch
                print(f"arc.log: dropped a batch of {len(batch)} log lines: {exc!r}", file=sys.stderr)
            finally:
                for _ in batch:
                    q.task_done()
            self._report_drops()

    def _deliver(self, batch: list[tuple[str, logging.LogRecord]]) -> None:
        groups: dict[str, tuple[list[str], list[logging.LogRecord]]] = {}
        for n, (category, record) in enumerate(batch, 1):
            try:
                line = self._formatter.format(record)
            except Exception:  # noqa: BLE001 - an unformattable record becomes a marker, not a crash
                line = json.dumps({"level": "error", "logger": record.name, "message": "<unformattable log record>"})
            lines, records = groups.setdefault(category, ([], []))
            lines.append(line)
            records.append(record)
            if n % _FORMAT_CHUNK == 0:
                time.sleep(0)  # hand the GIL to the event-loop thread
        for category, (lines, records) in groups.items():
            self._send(category, ("\n".join(lines) + "\n").encode("utf-8"), records)

    def _connection(self, category: str) -> socket.socket:
        conn = self._conns.get(category)
        if conn is None:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                conn.settimeout(self._send_timeout)
                conn.connect(self._socket_path)
                conn.sendall(f"CAT {category}\n".encode("ascii"))
            except OSError:
                conn.close()
                raise
            self._conns[category] = conn
        return conn

    def _drop_connection(self, category: str) -> None:
        conn = self._conns.pop(category, None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, category: str, blob: bytes, records: list[logging.LogRecord]) -> None:
        now = time.monotonic()
        if now >= self._down_until:
            try:
                self._connection(category).sendall(blob)
                self.sent += len(records)
                return
            except OSError:  # includes socket.timeout: the collector is gone or stalled
                self._drop_connection(category)
                self._down_until = now + self._backoff
        # Collector unavailable: write these records directly so nothing is lost.
        if self._fallback is None:
            self._fallback = self._fallback_factory()
        for record in records:
            self._fallback.emit(record)
        self.fallback_lines += len(records)

    def _report_drops(self) -> None:
        if self.dropped == self._reported_dropped:
            return
        now = time.monotonic()
        if now - self._last_drop_report >= _DROP_REPORT_SECONDS:
            self._last_drop_report = now
            lost = self.dropped - self._reported_dropped
            self._reported_dropped = self.dropped
            print(
                f"arc.log: {lost} log line(s) dropped because this process's log queue "
                f"({self._queue_size}) was full ({self.dropped} in total)",
                file=sys.stderr,
            )

    # -------------------------------------------------------------- lifecycle
    def flush(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.002)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._thread.join(timeout=5.0)  # drains the queue first; see _run
        for category in list(self._conns):
            self._drop_connection(category)
        if self._fallback is not None:
            self._fallback.close()
        super().close()

    def _reinit_after_fork(self) -> None:
        """The child of a fork inherits the queue contents, the open
        connections (shared with the parent: never write to them) and no sender
        thread. Start clean."""
        for conn in self._conns.values():
            try:
                conn.close()  # closes only this process's copy of the descriptor
            except OSError:
                pass
        self._conns = {}
        self._fallback = None
        self._down_until = 0.0
        self._queue = queue.Queue(maxsize=self._queue_size)
        if not self._closed:
            self._thread = self._start_thread()


def _after_fork_in_child() -> None:
    for handler in list(_live_collector_handlers):
        handler._reinit_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _collector_reachable(socket_path: str) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(socket_path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


# Logger names (and everything beneath them) whose records go to the rotating
# files only, never the console. A plugin with a chatty, per-request logger
# registers it here: on the console every line is serialised through a pipe
# into `arc run`'s single relay thread, which became the throughput ceiling
# (and the source of tens-of-milliseconds latency stalls) at a few thousand
# requests per second. The files lose nothing: _CategoryRouter has no filter.
_file_only_loggers: set[str] = set()


def file_only(logger_name: str) -> None:
    """Keep records from `logger_name` (and its children) off the console;
    they still go to their logs/<category>.jsonl. Idempotent."""
    _file_only_loggers.add(logger_name)


def console_too(logger_name: str) -> None:
    """Undo file_only(): records from `logger_name` reach the console again."""
    _file_only_loggers.discard(logger_name)


class _FileOnlyFilter(logging.Filter):
    """Console handler only (the file router has no filter): drops records
    from any logger registered with file_only(), matched by exact name or
    by a dotted-name ancestor, like logging's own hierarchy."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _file_only_loggers:
            return True
        name = record.name
        while True:
            if name in _file_only_loggers:
                return False
            dot = name.rfind(".")
            if dot < 0:
                return True
            name = name[:dot]


class _ExcludeAdvisoryFilter(logging.Filter):
    """Keeps Kernel.advise()'s "arc.advisory" records out of the console
    handler only — they still reach logs/system.jsonl via _CategoryRouter
    (no filter attached there). Needed because kernel.advise() now logs
    through both warnings.warn() (already suppressed at every arc.boot()
    call site, see gateway/_asgi_entrypoint.py) AND this logger; without
    this filter every advisory would reprint on console a second time,
    once per line, per worker process."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != "arc.advisory"


def configure(kernel: Any) -> None:
    """Called once per process, from arc.runtime.boot() — before any
    plugin's register() runs, so a plugin registration issue (kernel.advise,
    a genuine bug) already logs through this. Idempotent within a process:
    clears any handlers a prior configure() (or, in lineup's case, an
    earlier bare logging.basicConfig() call this replaces) already
    attached, so a second boot() in the same process (tests, `force=True`)
    never double-attaches handlers and double-prints every line.

    The file sink is the log collector when `arc run` started one (its socket
    path is in ARC_LOG_SOCKET and it answers), otherwise this process writes
    logs/ itself."""
    global _router, _sink

    cfg = read_sink_settings(kernel.settings)

    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(cfg.level)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_CONSOLE_DATEFMT))
    console.addFilter(_ExcludeAdvisoryFilter())
    console.addFilter(_FileOnlyFilter())
    root.addHandler(console)

    logs_dir = (kernel.project_root or Path.cwd()) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    _sink = (logs_dir, cfg)
    handler = None
    socket_path = os.environ.get(COLLECTOR_ENV)
    if socket_path:
        handler = _collector_handler(socket_path)
    _router = handler or _direct_router()
    root.addHandler(_router)


def _direct_router(name_suffix: str = "") -> _CategoryRouter:
    assert _sink is not None
    logs_dir, cfg = _sink
    return _CategoryRouter(
        logs_dir, max_bytes=cfg.max_bytes, backup_count=cfg.backup_count, name_suffix=name_suffix
    )


def _fallback_router() -> _CategoryRouter:
    """Where a process writes when the collector is unreachable: files of its
    OWN (`<category>.fallback-<pid>.jsonl`), never the shared `<category>.jsonl`.
    The shared files belong to the collector; several processes rotating them
    at once is the race the collector exists to remove, and it would also push
    the collector's already-written lines out of the backup window (measured:
    48% of lines kept when the collector was killed mid-load). A file with one
    owner rotates safely. The pid is read when the fallback is first needed, so
    a forked child gets its own file."""
    return _direct_router(name_suffix=f".fallback-{os.getpid()}")


def _collector_handler(socket_path: str) -> _CollectorHandler | None:
    """A handler talking to the collector at `socket_path`, or None if nothing
    is listening there (a stale ARC_LOG_SOCKET, a collector that already
    exited): the caller then writes the files itself."""
    assert _sink is not None
    if not _collector_reachable(socket_path):
        return None
    _, cfg = _sink
    return _CollectorHandler(
        socket_path, fallback_factory=_fallback_router, send_interval=cfg.flush_seconds
    )


def use_collector(socket_path: str | os.PathLike[str]) -> bool:
    """Switch THIS process's file sink to the collector at `socket_path` (what
    `arc run` does for itself right after starting the collector — it booted
    before the collector existed). False if there is no sink to replace or the
    collector isn't answering; the direct sink then stays."""
    global _router
    if _sink is None:
        return False
    handler = _collector_handler(os.fspath(socket_path))
    if handler is None:
        return False
    root = logging.getLogger()
    root.addHandler(handler)  # add before removing: no record falls between the two
    old, _router = _router, handler
    if old is not None:
        root.removeHandler(old)
        old.close()
    return True


def use_direct() -> None:
    """Switch THIS process's file sink back to writing logs/ directly, flushing
    the collector handler first (`arc run` calls this before stopping the
    collector, so its own last records are not stranded)."""
    global _router
    if _sink is None or not isinstance(_router, _CollectorHandler):
        return
    root = logging.getLogger()
    handler = _direct_router()
    root.addHandler(handler)
    old, _router = _router, handler
    root.removeHandler(old)
    old.close()  # drains its queue to the collector (or to the fallback files)


def stats() -> dict[str, Any]:
    """What this process's file sink has done: mode, and for the collector
    handler how many lines it sent, wrote directly (fallback) or dropped."""
    if isinstance(_router, _CollectorHandler):
        return {
            "mode": "collector",
            "sent": _router.sent,
            "fallback_lines": _router.fallback_lines,
            "dropped": _router.dropped,
        }
    return {"mode": "direct"}


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
