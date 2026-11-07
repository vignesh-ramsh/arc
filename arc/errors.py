"""
arc.errors
-----------------
Error tracking: one place every unhandled exception in a running ARC process goes, so that "something
is failing" is something you can see, count and be told about, instead of a traceback in a log file
nobody is reading.

    arc.errors.capture(exc, source="gateway", route="/files/upload", request_id=rid)

The gateway calls it for every 500, lineup for every failed job and for a worker that dies; a plugin
calls it from its own `except` blocks for failures it handles but wants to know about. It never raises
and never blocks: the caller is already handling a failure and must not get another one from the
machinery that reports it.

What a capture does, always (no configuration):

  * counts it, per source: `arc_errors_total` / `arc_errors_by_source_total{bucket="gateway"}` on
    `/metrics`, summed over every process like the other counters, so an alert is one Prometheus rule
    (`increase(arc_errors_total[5m]) > 0`);
  * writes ONE line to `logs/errors.jsonl`: when, which process type, exception type, a message with
    secrets removed, the last frames (file:line in function; no local variables), a fingerprint and
    the context the caller gave (route template, queue, job, request id). The fingerprint is the same
    for the same bug whatever data triggered it (exception type + innermost frame in project code),
    which is what lets `arc errors` count distinct problems rather than occurrences.

What it does when `error_tracking_dsn` is set (a Sentry or GlitchTip project): also sends the event
there, once the sentry-sdk package is installed (`uv add sentry-sdk`; it is not a dependency of ARC).
The SDK is configured to send as little as possible: no request headers, cookies, bodies or client
address, no local variables, no user, and the exception message passes through the same scrubbing as
the log. At most `error_tracking_max_per_minute` events per fingerprint reach the tracker (default 10;
the rest are still counted and logged), so one bug in a hot path cannot burn the quota.

Further destinations are one function: `arc.errors.add_sink(lambda event: ...)`. A sink gets the
`ErrorEvent` and must not block (it runs on the thread that hit the error, often the event loop).

Not covered: an exception nothing catches in a process this module is not called from (a plugin's own
background task that dies silently); the traceback logged by `logger.exception` is still in the
category's log file.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import re
import threading
import time
import traceback
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("arc.errors")

DSN_KEY = "error_tracking_dsn"
ENVIRONMENT_KEY = "error_tracking_environment"
MAX_PER_MINUTE_KEY = "error_tracking_max_per_minute"
DEFAULT_ENVIRONMENT = "production"
DEFAULT_MAX_PER_MINUTE = 10

_MAX_FRAMES = 12
_MAX_MESSAGE = 500
_MAX_CONTEXT_VALUE = 200
_MAX_FINGERPRINTS = 1000  # distinct fingerprints remembered for rate limiting and counting
_MIN_SECRET_LENGTH = 8  # a shorter "secret" would blank out ordinary words
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ENV_PYTHON = re.compile(r"(?:^|/)(?:site-packages|dist-packages)/(.+)$")


@dataclass(frozen=True)
class ErrorEvent:
    """What a sink receives. `exception` is the live exception (a sink that wants a real stack
    trace needs it); everything else is already scrubbed and safe to store or send."""

    timestamp: float
    source: str
    error_type: str
    message: str
    fingerprint: str
    frames: tuple[str, ...]
    context: dict[str, Any]
    exception: BaseException = field(repr=False, compare=False)


Sink = Callable[[ErrorEvent], None]

_lock = threading.Lock()
_sentry_lock = threading.Lock()  # separate: the SDK is started while this is held, and that may log through _lock
_kernel: Any = None
_sinks: list[Sink] = []
_total = 0
_by_source: Counter[str] = Counter()
_by_fingerprint: Counter[str] = Counter()
_suppressed = 0
_recent: dict[str, deque[float]] = {}  # fingerprint -> times of the events let through, last minute
_sink_failed_at: dict[str, float] = {}
_sentry_state: dict[str, Any] = {"tried": False, "sdk": None}


# --------------------------------------------------------------------------- #
# Boot
# --------------------------------------------------------------------------- #
def declare(kernel: Any) -> None:
    """Declares the settings and remembers the kernel (for the project root and the secrets to
    scrub). Called by arc.boot() before plugins register. Cheap: it neither imports the SDK nor
    contacts anything; that waits for the first error."""
    global _kernel
    from . import log as _log

    _kernel = kernel
    _log.file_only("arc.errors")  # one line per error in logs/errors.jsonl; not echoed to every console
    kernel.settings.declare(
        DSN_KEY,
        secret=True,
        doc="Sentry/GlitchTip DSN to send errors to (needs `uv add sentry-sdk`). Unset = errors are "
        "only counted and written to logs/errors.jsonl. Restart to apply.",
    )
    kernel.settings.declare(
        ENVIRONMENT_KEY,
        default=DEFAULT_ENVIRONMENT,
        doc="The environment name errors are filed under at the tracker (production, staging, ...).",
    )
    kernel.settings.declare(
        MAX_PER_MINUTE_KEY,
        type=int,
        default=DEFAULT_MAX_PER_MINUTE,
        doc="Most events per fingerprint per minute sent to the tracker; the rest are only counted "
        "and logged. 0 = no limit.",
    )


def sentry_missing_notice(kernel: Any) -> str | None:
    """A sentence for boot to advise when a DSN is configured but the SDK cannot be imported (the
    errors would silently go nowhere); None when there is nothing to say."""
    try:
        dsn = kernel.settings.get(DSN_KEY, reveal=True)
    except Exception:  # noqa: BLE001 - a settings problem is reported by its own check; this is advice
        return None
    if not dsn:
        return None
    try:
        import sentry_sdk  # noqa: F401
    except ImportError:
        return (
            f"{DSN_KEY} is set but the sentry-sdk package is not installed, so errors are NOT being "
            f"sent anywhere (they are still counted and written to logs/errors.jsonl). "
            f"Install it with `uv add sentry-sdk`."
        )
    return None


# --------------------------------------------------------------------------- #
# Capturing
# --------------------------------------------------------------------------- #
def capture(exc: BaseException, *, source: str, **context: Any) -> str | None:
    """Record an error. `source` is the kind of process or subsystem (gateway, lineup, ...);
    `context` is small facts that help find it: a route TEMPLATE (never the raw path with ids in it),
    a queue, a job name, a request id. Returns the fingerprint, or None if it could not be recorded.
    Never raises."""
    try:
        event = _build(exc, source, context)
    except Exception as failure:  # noqa: BLE001 - reporting an error must not become a second error
        _warn_rarely("build", f"could not describe an error from {source!r}: {type(failure).__name__}: {failure}")
        return None
    _record(event)
    return event.fingerprint


def _build(exc: BaseException, source: str, context: dict[str, Any]) -> ErrorEvent:
    frames = _frames(exc)
    return ErrorEvent(
        timestamp=time.time(),
        source=str(source)[:64],
        error_type=f"{type(exc).__module__}.{type(exc).__qualname__}"
        if type(exc).__module__ not in ("builtins", "__main__")
        else type(exc).__qualname__,
        message=scrub(_message(exc)),
        fingerprint=_fingerprint(exc, frames),
        frames=tuple(_describe(frame) for frame in frames[-_MAX_FRAMES:]),
        context=_clean_context(context),
        exception=exc,
    )


def _record(event: ErrorEvent) -> None:
    global _total, _suppressed
    limit = _limit_per_minute()
    with _lock:
        _total += 1
        _by_source[event.source] += 1
        if event.fingerprint in _by_fingerprint or len(_by_fingerprint) < _MAX_FINGERPRINTS:
            _by_fingerprint[event.fingerprint] += 1
        send = _allow(event.fingerprint, event.timestamp, limit)
        if not send:
            _suppressed += 1
        sinks = list(_sinks)
    try:
        logger.error(
            "%s: %s",
            event.error_type,
            event.message,
            extra={
                "source": event.source,
                "error_type": event.error_type,
                "error_message": event.message,
                "fingerprint": event.fingerprint,
                "frames": list(event.frames),
                "error_context": event.context,
                "forwarded": send,
            },
        )
    except Exception as failure:  # noqa: BLE001 - a logging failure must not break the caller
        _warn_rarely("log", f"could not write the error log line: {type(failure).__name__}: {failure}")
    if not send:
        return
    for sink in (*sinks, _sentry_sink):
        name = getattr(sink, "__name__", repr(sink))
        try:
            sink(event)
        except Exception as failure:  # noqa: BLE001 - one broken destination must not stop the others or the caller
            _warn_rarely(name, f"error sink {name} failed: {type(failure).__name__}: {failure}")


def _limit_per_minute() -> int:
    try:
        return int(_kernel.settings.get(MAX_PER_MINUTE_KEY)) if _kernel is not None else DEFAULT_MAX_PER_MINUTE
    except Exception:  # noqa: BLE001 - an unreadable setting means the default, not a lost error
        return DEFAULT_MAX_PER_MINUTE


def _allow(fingerprint: str, now: float, limit: int) -> bool:
    """The rate limit on what reaches sinks: at most `limit` per fingerprint in any 60 seconds. Caller
    holds the lock."""
    if limit <= 0:
        return True
    window = _recent.get(fingerprint)
    if window is None:
        if len(_recent) >= _MAX_FINGERPRINTS:
            _recent.pop(next(iter(_recent)))  # the oldest-seen fingerprint; plain dicts keep insertion order
        window = _recent[fingerprint] = deque()
    while window and now - window[0] >= 60:
        window.popleft()
    if len(window) >= limit:
        return False
    window.append(now)
    return True


def _warn_rarely(key: str, message: str) -> None:
    """A problem with error tracking itself, said at most once a minute per kind: when it is broken
    it is broken for every error, and a line per error would bury the real ones."""
    now = time.monotonic()
    with _lock:
        last = _sink_failed_at.get(key)
        if last is not None and now - last < 60:
            return
        _sink_failed_at[key] = now
    logging.getLogger("arc").warning(message)


# --------------------------------------------------------------------------- #
# Describing an exception: what it is, where, without the data that triggered it
# --------------------------------------------------------------------------- #
def _frames(exc: BaseException) -> list[traceback.FrameSummary]:
    return list(traceback.extract_tb(exc.__traceback__))


def _is_project_code(filename: str) -> bool:
    """Code we wrote (the project, arc, its plugins) as opposed to the standard library or an
    installed third party: the frame whose function name is a better identity for a bug."""
    return not (_ENV_PYTHON.search(filename.replace("\\", "/")) or "/lib/python" in filename.replace("\\", "/"))


def _fingerprint(exc: BaseException, frames: list[traceback.FrameSummary]) -> str:
    """The same for the same bug, whatever data triggered it: exception type plus the innermost frame
    in project code (file and function, not line number - an edit elsewhere in the file would
    otherwise make every old error look new). Falls back to the innermost frame of any kind."""
    innermost = next((f for f in reversed(frames) if _is_project_code(f.filename)), frames[-1] if frames else None)
    where = f"{_relative(innermost.filename)}:{innermost.name}" if innermost is not None else "-"
    return hashlib.sha1(f"{type(exc).__qualname__}|{where}".encode()).hexdigest()[:12]  # noqa: S324 - an identifier, not security


def _relative(filename: str) -> str:
    """The path with the machine-specific part removed: relative to the project when it is inside it,
    `pkg/mod.py` for an installed package, else the bare file name."""
    path = filename.replace("\\", "/")
    installed = _ENV_PYTHON.search(path)
    if installed:
        return installed.group(1)
    if _kernel is not None:
        root = str(Path(_kernel.project_root).resolve()).replace("\\", "/")
        if path.startswith(root + "/"):
            return path[len(root) + 1 :]
    return path.rsplit("/", 1)[-1]


def _describe(frame: traceback.FrameSummary) -> str:
    return f"{_relative(frame.filename)}:{frame.lineno} in {frame.name}"


def _message(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 - an exception whose own __str__ fails is still an error worth recording
        return "<message unavailable>"


def scrub(text: str) -> str:
    """`text` with every secret setting's value replaced, e-mail addresses masked, and the length
    capped: an exception message often quotes the data that caused it ("Key (email)=(a@b.com)
    already exists"), and it is about to be stored and possibly sent to a third party."""
    if _kernel is not None:
        try:
            values = _kernel.settings.secret_values_for_redaction()
        except Exception:  # noqa: BLE001 - without the list, still mask what can be recognised
            values = []
        for value in values:
            if isinstance(value, str) and len(value) >= _MIN_SECRET_LENGTH:
                text = text.replace(value, "***REDACTED***")
    text = _EMAIL.sub("[email]", text)
    return text if len(text) <= _MAX_MESSAGE else text[: _MAX_MESSAGE - 1] + "…"


def _clean_context(context: dict[str, Any]) -> dict[str, Any]:
    """Only small plain facts: a value that is not text, a number, a bool or None is described by its
    type rather than stored (a request body or a model instance does not belong in an error line)."""
    cleaned: dict[str, Any] = {}
    for key, value in context.items():
        if value is None or isinstance(value, (bool, int, float)):
            cleaned[str(key)[:64]] = value
        elif isinstance(value, str):
            cleaned[str(key)[:64]] = scrub(value)[:_MAX_CONTEXT_VALUE]
        else:
            cleaned[str(key)[:64]] = f"<{type(value).__name__}>"
    return cleaned


# --------------------------------------------------------------------------- #
# Destinations
# --------------------------------------------------------------------------- #
def add_sink(sink: Sink) -> None:
    """Also hand every (rate-limited) event to `sink(event)`. It runs on the thread that hit the
    error, so it must not block; an exception it raises is logged once a minute and otherwise
    ignored."""
    with _lock:
        if sink not in _sinks:
            _sinks.append(sink)


def remove_sink(sink: Sink) -> None:
    with _lock:
        if sink in _sinks:
            _sinks.remove(sink)


def _sentry_sdk() -> Any | None:
    """The sentry_sdk module, initialised for this process, or None when there is no DSN or no SDK.
    Done on the first error, not at boot: a process that never fails never imports it."""
    if _sentry_state["tried"]:
        return _sentry_state["sdk"]
    with _sentry_lock:
        if _sentry_state["tried"]:
            return _sentry_state["sdk"]
        _sentry_state["tried"] = True
        if _kernel is None:
            return None
        try:
            dsn = _kernel.settings.get(DSN_KEY, reveal=True)
            if not dsn:
                return None
            import sentry_sdk

            sentry_sdk.init(**sentry_options(dsn, _kernel))
        except ImportError:
            _warn_rarely("sentry-import", sentry_missing_notice(_kernel) or "sentry-sdk is not installed")
            return None
        except Exception as failure:  # noqa: BLE001 - a bad DSN must not turn every error into a crash
            _warn_rarely("sentry-init", f"could not start error tracking: {type(failure).__name__}: {failure}")
            return None
        _sentry_state["sdk"] = sentry_sdk
        # The SDK sends from a background thread, and the integration that flushes it as the
        # interpreter exits is one of the defaults switched off in sentry_options(): without this, a
        # process that dies of an error (the case that matters most) would exit before its event left.
        atexit.register(flush)
        return sentry_sdk


def flush(timeout: float = 2.0) -> None:
    """Wait up to `timeout` seconds for events already handed to the tracker to be sent. Call it
    before a process exits on purpose (a worker that crashed); it is also registered to run at
    interpreter exit. Does nothing when no tracker was started."""
    sdk = _sentry_state["sdk"]
    if sdk is None:
        return
    try:
        sdk.flush(timeout=timeout)
    except Exception as failure:  # noqa: BLE001 - a flush that fails must not stop the process from exiting
        _warn_rarely("sentry-flush", f"could not flush error tracking: {type(failure).__name__}: {failure}")


def sentry_options(dsn: str, kernel: Any) -> dict[str, Any]:
    """What the SDK is started with. Everything that could carry personal data is switched off, and
    only events captured by `capture` are sent: no automatic integrations that would also send every
    logged error, no tracing."""
    release = None
    try:
        import arc as arc_package

        release = f"arc@{getattr(arc_package, '__version__', 'unknown')}"
    except Exception:  # noqa: BLE001 - the release label is a nicety
        pass
    return {
        "dsn": dsn,
        "environment": kernel.settings.get(ENVIRONMENT_KEY) or DEFAULT_ENVIRONMENT,
        "release": release,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "default_integrations": False,
        "auto_enabling_integrations": False,
        "traces_sample_rate": 0.0,
        "before_send": before_send,
    }


def before_send(event: dict[str, Any], hint: Any = None) -> dict[str, Any] | None:
    """Last look at what the SDK built, before it leaves the machine: drops anything about the request
    or the person, local variables, and the machine's file paths, and scrubs the exception messages."""
    for key in ("request", "user", "extra", "breadcrumbs", "modules", "contexts"):
        event.pop(key, None)
    for exception in (event.get("exception") or {}).get("values") or []:
        if isinstance(exception.get("value"), str):
            exception["value"] = scrub(exception["value"])
        for frame in (exception.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
            frame.pop("pre_context", None)
            frame.pop("context_line", None)
            frame.pop("post_context", None)
            if isinstance(frame.get("abs_path"), str):
                frame["abs_path"] = _relative(frame["abs_path"])
    if isinstance(event.get("message"), str):
        event["message"] = scrub(event["message"])
    return event


def _sentry_sink(event: ErrorEvent) -> None:
    sdk = _sentry_sdk()
    if sdk is None:
        return
    scope_factory = getattr(sdk, "new_scope", None) or sdk.push_scope  # sentry-sdk 2.x, else 1.x
    with scope_factory() as scope:
        scope.set_tag("source", event.source)
        for key, value in event.context.items():
            scope.set_tag(key, str(value))
        scope.fingerprint = [event.fingerprint]
        sdk.capture_exception(event.exception)


# --------------------------------------------------------------------------- #
# Numbers, for /metrics (arc.metrics.collect)
# --------------------------------------------------------------------------- #
def metrics() -> dict[str, Any]:
    """This process's counters, in the shape arc.metrics aggregates over every process: `total`
    (all captures), `by_source_total` (the same by source), `unforwarded_total` (captures held back
    from the tracker by the per-fingerprint rate limit)."""
    with _lock:
        return {
            "total": _total,
            "by_source_total": dict(_by_source),
            "unforwarded_total": _suppressed,
        }


def top(limit: int = 10) -> list[tuple[str, int]]:
    """This process's most frequent fingerprints, for tests and debugging; the cross-process view is
    `arc errors`, from the log."""
    with _lock:
        return _by_fingerprint.most_common(limit)


def reset() -> None:
    """Forget counters, rate-limit state and the SDK state (tests, and embedding). Sinks stay."""
    global _total, _suppressed, _kernel
    with _lock:
        _total = _suppressed = 0
        _by_source.clear()
        _by_fingerprint.clear()
        _recent.clear()
        _sink_failed_at.clear()
        _sentry_state.update(tried=False, sdk=None)
        _kernel = None
