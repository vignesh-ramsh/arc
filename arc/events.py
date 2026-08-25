"""
arc.events
-----------------
Process-local event bus + process-notification bridge (docs/
arc-kernel-event-process-notification-proposal.md). The fourth Kernel
service module alongside arc.codec/arc.health/arc.settings.

Two deliberately separate things live here, per the proposal:

1. THE EVENT BUS — `on()` / `emit()` / `subscriptions()`. Strictly
   process-local (§5/§15 of the proposal): no cross-process delivery, no
   persistence, no retry, no ordering promises beyond "sequential, in
   subscription order, within this process". Namespaced names
   ("pgdb.schema.changed"); the `system.` prefix is reserved for the
   framework by convention (documented, not hard-enforced — emit-time
   plugin attribution doesn't exist outside register(), §3.1).
   Domain-blind exactly like arc.health: the Kernel dispatches names to
   handlers, it never knows what any event means.

   Handler semantics (the part the proposal left unstated, settled here):
   dispatch is SEQUENTIAL in subscription order (deterministic, matching
   relay's own hook-ordering precedent — never gather(), whose
   interleaving surprises); a handler that raises is logged and skipped,
   never allowed to stop later handlers or propagate to the emitter
   (arc.health.check()'s exact per-capability posture). Corollary: an
   emitter must NEVER depend on a subscriber's outcome — if you need the
   outcome, that's a direct call or a relay hook, not an event.

2. THE PROCESS BRIDGE — `install_process_bridge()`, opt-in and explicitly
   NEVER called by arc.boot() (boot is read-only and runs in CLIs/tests/
   scripts; signal handlers and background tasks are process-global state
   only a long-running entrypoint should claim). Installed by gateway's
   ASGI lifespan and lineup's worker/scheduler CLIs. It provides the two
   cross-process triggers, both funneling into ONE local event,
   `system.reload` ("reconcile your reloadable state with its source of
   truth" — §6 of the proposal; persistent storage is always the truth,
   §9, so a missed/coalesced trigger is harmless):

     * SIGUSR1 — the instant push path (`arc reload`, or any supervisor's
       `kill --signal=SIGUSR1`). Registered defensively: signal handlers
       only work on the main thread, and some server runtimes run worker
       event loops elsewhere — if neither loop.add_signal_handler nor
       signal.signal can be installed, the bridge degrades to poll-only
       with a log line, never a crash.
     * THE RELOAD-STAMP POLL — the automatic path, and the reason no
       optional plugin (redix) is ever needed for correctness (§14): any
       capability MAY expose `async def reload_stamp() -> Any` (duck-typed,
       same shape as health()); the bridge polls every stamp every few
       seconds and emits system.reload when any changes. pgdb's stamp is
       max(applied_at) from _patch_history — meaning every schema apply,
       from ANY process (`arc pgdb migrate`, admin's Apply Now), is
       noticed by every bridge-running process within one poll interval,
       with zero configuration. The Kernel never knows what a stamp means
       — only whether it changed. The first poll records a baseline
       without emitting (boot already read fresh state).

   Installing also registers this process in `.arc/runtime/processes/`
   (pid + role), which is what `arc ps` lists and `arc reload` signals —
   only registered processes ever receive SIGUSR1 from `arc reload`, so a
   process without a handler (default disposition: terminate!) can never
   be hit by it.

Code changes still require a real restart (§12) — nothing here hot-reloads
Python. `arc restart` (cli.py) runs the deployment-supplied
`restart_command` setting; the Kernel itself stays supervisor-blind (§13).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import signal as _signal
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import _state
from .kernel import Kernel, KernelError

_logger = logging.getLogger("arc.events")

RELOAD_EVENT = "system.reload"
#: The one OS signal the bridge claims. SIGUSR1/SIGUSR2 are the POSIX
#: application-defined signals; SIGHUP is deliberately avoided (Granian's
#: own master already claims it for its --reload feature).
BRIDGE_SIGNAL = getattr(_signal, "SIGUSR1", None)

EventHandler = Callable[..., Awaitable[Any]]


class EventsError(KernelError):
    """arc.events was used before arc.boot() — no active kernel to hold
    the subscription registry."""


# ---------------------------------------------------------------------- #
# Subscription registry — per-Kernel, via WeakKeyDictionary, so a re-boot
# (force=True) or shutdown() naturally starts with a clean registry and a
# stale kernel's subscriptions can never double-fire. Same lifetime story
# as Kernel.advisories, without kernel.py needing to know events exist.
# ---------------------------------------------------------------------- #
_registries: "weakref.WeakKeyDictionary[Kernel, dict[str, list[tuple[str, EventHandler]]]]" = (
    weakref.WeakKeyDictionary()
)


def _registry() -> dict[str, list[tuple[str, EventHandler]]]:
    kernel = _state.get_kernel()
    if kernel is None:
        raise EventsError(
            "arc.events requires arc.boot() first — there is no active kernel "
            "to hold event subscriptions."
        )
    return _registries.setdefault(kernel, {})


def on(name: str, handler: EventHandler) -> EventHandler:
    """Subscribe `handler` (async) to event `name` (exact match — no
    wildcards, deliberately bounded for v1). Returns the handler so it can
    be used as a plain call or a decorator. Typically called from a
    plugin's own register(kernel), where Kernel.current_plugin() still
    attributes the subscription for subscriptions()' introspection."""
    if not asyncio.iscoroutinefunction(handler):
        # "await is never hidden" (docs/arc.MD §3.11) — same async-only
        # convention hooks already enforce by shape.
        raise TypeError(f"event handler for '{name}' must be `async def` — got {handler!r}.")
    kernel = _state.get_kernel()
    plugin = (kernel.current_plugin() if kernel else None) or "<direct>"
    _registry().setdefault(name, []).append((plugin, handler))
    return handler


async def emit(name: str, **payload: Any) -> dict[str, str]:
    """Dispatch `name` to every subscribed handler in THIS process, in
    subscription order. Returns {handler description: "ok" | "error: ..."}
    — informational only; failures are logged here and never propagate to
    the emitter (see module docstring for why)."""
    results: dict[str, str] = {}
    event_stats = _stats_registry().setdefault(name, {})
    for plugin, handler in list(_registry().get(name, ())):
        desc = f"{plugin}.{getattr(handler, '__name__', repr(handler))}"
        counters = event_stats.setdefault(
            desc, {"ok": 0, "error": 0, "last_error": None, "last_error_at": None}
        )
        try:
            await handler(**payload)
            results[desc] = "ok"
            counters["ok"] += 1
        except Exception as exc:
            _logger.exception("event '%s': handler %s raised — continuing", name, desc)
            results[desc] = f"error: {type(exc).__name__}: {exc}"
            counters["error"] += 1
            counters["last_error"] = results[desc]
            counters["last_error_at"] = time.time()
    return results


def subscriptions() -> dict[str, list[str]]:
    """Introspection: {event name: ["plugin.handler_name", ...]} — the
    events counterpart of arc.gateway.routes()/arc.relay.whitelisted()."""
    return {
        name: [f"{plugin}.{getattr(h, '__name__', repr(h))}" for plugin, h in handlers]
        for name, handlers in _registry().items()
    }


# ---------------------------------------------------------------------- #
# File loader — the on()/emit() counterpart of relay.register_hooks()/
# register_api() and pgdb.register_model()/register_patches(): every one
# of those takes an arbitrary Path, no-ops if it doesn't exist, and loads
# whatever's there under a synthetic module name via
# importlib.util.spec_from_file_location — never a real Python package
# import. register_from() lets a plugin keep its `arc.events.on(...)`
# subscriptions in their own file (e.g. events.py) at the plugin's own
# root, right alongside api/hooks/schemas, without that file needing to
# sit inside the plugin's importable package. Purely mechanical, same as
# its siblings — it never knows or cares what the loaded module's
# register(kernel) actually subscribes to. No manual plugin-attribution
# bookkeeping needed here: register_from() is only ever called from
# within a plugin's own top-level register(kernel), so
# Kernel.current_plugin() (which on() already reads internally) is
# already correct for the whole nested call, exactly like every other
# helper a plugin's register() calls.
# ---------------------------------------------------------------------- #
def register_from(path: str | Path) -> None:
    """Load a single Python file by path and call its own `register(kernel)`
    — e.g. `arc.events.register_from(Path(__file__).parent.parent /
    "events.py")` from a plugin's register(kernel). No-ops if `path`
    doesn't exist, matching every other directory/file-based loader in
    ARC. Raises EventsError if the file exists but defines no callable
    `register(kernel)` — an authoring mistake worth failing loudly on,
    the same posture a plugin's own missing entry point would get."""
    path = Path(path)
    if not path.exists():
        return
    kernel = _state.get_kernel()
    if kernel is None:
        raise EventsError("arc.events.register_from() requires arc.boot() first.")
    plugin = kernel.current_plugin() or "<direct>"
    module_name = f"_arc_events_{plugin}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    register_fn = getattr(module, "register", None)
    if not callable(register_fn):
        raise EventsError(
            f"'{path}' was loaded via arc.events.register_from() but defines no "
            f"register(kernel) function — every module loaded this way must define one."
        )
    register_fn(kernel)


# ---------------------------------------------------------------------- #
# Failure counters — per-Kernel, same WeakKeyDictionary lifetime as the
# subscription registry above, so a re-boot starts at zero. Cumulative
# THIS PROCESS's own in-memory counts since it booted — never persisted,
# never shared across processes. That's precisely why `arc doctor` (a
# separate, short-lived CLI process — see doctor.py) never reads this: a
# fresh doctor invocation boots its own fresh kernel with an empty
# registry, so it would only ever be able to report "zero failures",
# forever, regardless of what the real running application has actually
# seen. stats() exists to be read from WITHIN a live process instead —
# wire it into your own health endpoint or admin page if you want
# visibility into it, the same way subscriptions() already works.
# ---------------------------------------------------------------------- #
_stats: weakref.WeakKeyDictionary[Kernel, dict[str, dict[str, dict[str, Any]]]] = (
    weakref.WeakKeyDictionary()
)


def _stats_registry() -> dict[str, dict[str, dict[str, Any]]]:
    kernel = _state.get_kernel()
    if kernel is None:
        raise EventsError("arc.events requires arc.boot() first — there is no active kernel.")
    return _stats.setdefault(kernel, {})


def stats() -> dict[str, dict[str, dict[str, Any]]]:
    """Cumulative emit() outcomes for THIS process's kernel, since it
    booted: {event name: {"plugin.handler": {"ok": n, "error": n,
    "last_error": str | None, "last_error_at": float | None}}}. In-memory
    only — see the block comment above for why `arc doctor` deliberately
    never calls this."""
    return {
        name: {desc: dict(counters) for desc, counters in handlers.items()}
        for name, handlers in _stats_registry().items()
    }


# ---------------------------------------------------------------------- #
# Process registry — `.arc/runtime/processes/<pid>.json`. What `arc ps`
# lists and `arc reload` signals. Pure bookkeeping files; liveness is
# always re-checked with kill(pid, 0) at read time, so a crash that never
# removed its file only ever leaves a stale entry that the next reader
# prunes — never a wrong signal target.
# ---------------------------------------------------------------------- #
def _processes_dir(project_root: Path) -> Path:
    return project_root / ".arc" / "runtime" / "processes"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, someone else's — shouldn't happen for .arc-local pids
        return True


def register_process(project_root: Path, *, role: str) -> Path:
    directory = _processes_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{os.getpid()}.json"
    path.write_text(json.dumps({"pid": os.getpid(), "role": role, "started_at": time.time()}))
    return path


def unregister_process(project_root: Path) -> None:
    with contextlib.suppress(OSError):
        (_processes_dir(project_root) / f"{os.getpid()}.json").unlink(missing_ok=True)


def list_processes(project_root: Path, *, prune: bool = True) -> list[dict]:
    """Live registered processes. `prune=True` (default) also deletes
    entries whose pid is gone — self-healing after any hard kill."""
    directory = _processes_dir(project_root)
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            info = json.loads(path.read_text())
            pid = int(info["pid"])
        except (ValueError, KeyError, json.JSONDecodeError):
            if prune:
                with contextlib.suppress(OSError):
                    path.unlink()
            continue
        if not _pid_alive(pid):
            if prune:
                with contextlib.suppress(OSError):
                    path.unlink()
            continue
        out.append(info)
    return out


# ---------------------------------------------------------------------- #
# arc run singleton lock — `.arc/runtime/arc_run.lock`. Distinct from the
# process registry above (which tracks EVERY long-running process: one
# entry per gateway worker, per lineup worker, plus the scheduler) — this
# is ONE lock for the ORCHESTRATOR itself (`arc run`, cli.py's own
# module), so a second `arc run` invocation against the same project can
# never start alongside a first one that's still alive — competing for
# the same port, double-consuming the same lineup queues, or (the actual
# incident this closes) a stuck/stopped first instance silently squatting
# on the port while a second one is launched on top of it. Same self-
# healing posture as the process registry above: liveness is re-checked
# with kill(pid, 0) at acquire time, so a crash that left the lock file
# behind is never mistaken for a real conflict — only a genuinely live
# holder blocks a new acquire.
# ---------------------------------------------------------------------- #
class RunLockError(KernelError):
    """Another `arc run` is already active for this project."""


def _run_lock_path(project_root: Path) -> Path:
    return project_root / ".arc" / "runtime" / "arc_run.lock"


def acquire_run_lock(project_root: Path) -> Path:
    """Claim the one-`arc run`-per-project lock, or raise RunLockError
    naming the live holder's pid. A stale lock (holder's pid no longer
    alive, e.g. a crash that skipped release_run_lock) is silently
    reclaimed — never treated as a conflict, the same posture
    list_processes()'s own pruning already takes."""
    path = _run_lock_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            info = json.loads(path.read_text())
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pid = None
        if pid is not None and pid != os.getpid() and _pid_alive(pid):
            raise RunLockError(
                f"another `arc run` is already active for this project (pid {pid}). "
                f"Stop it first (Ctrl-C in its terminal, or `kill {pid}`), then try "
                f"again — or delete '{path}' directly if you're certain it's stale."
            )
    path.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}))
    return path


def release_run_lock(project_root: Path) -> None:
    """Safe to call even if acquire_run_lock never ran (nothing to
    remove) or another process already reclaimed a stale lock."""
    with contextlib.suppress(OSError):
        _run_lock_path(project_root).unlink(missing_ok=True)


# ---------------------------------------------------------------------- #
# The process bridge. Module-level state, not per-Kernel: OS signal
# dispositions and the watcher task are process-global no matter what;
# the watcher reads the CURRENT kernel through _state on every tick, so a
# re-boot under it is harmless.
# ---------------------------------------------------------------------- #
_SIGNAL_TICK_SECONDS = 0.5  # how often the watcher notices a pending SIGUSR1

_bridge_task: asyncio.Task | None = None
_bridge_signal_flag = threading.Event()
_bridge_signal_installed = False
_bridge_pidfile: Path | None = None


def _stamp_capabilities() -> dict[str, Any]:
    """Duck-typed sweep, exactly like arc.health.check(): any capability
    exposing `reload_stamp` participates; everything else is skipped."""
    kernel = _state.get_kernel()
    if kernel is None:
        return {}
    return {
        name: cap.instance
        for name, cap in kernel.capabilities().items()
        if callable(getattr(cap.instance, "reload_stamp", None))
    }


async def _read_stamps() -> dict[str, Any]:
    stamps: dict[str, Any] = {}
    for name, instance in _stamp_capabilities().items():
        try:
            stamps[name] = await instance.reload_stamp()
        except Exception as exc:
            # A capability that can't answer right now (pool not open yet,
            # table not bootstrapped) simply doesn't vote this tick.
            _logger.debug("reload_stamp on '%s' failed (%s) — skipping this tick", name, exc)
    return stamps


async def _bridge_loop(poll_interval: float) -> None:
    # Baseline WITHOUT emitting: this process just booted, so it already
    # read every source of truth fresh — an emit here would be a pointless
    # reload storm on every worker start.
    baseline = await _read_stamps()
    last_poll = time.monotonic()
    while True:
        await asyncio.sleep(_SIGNAL_TICK_SECONDS)
        fire = False
        reason = ""

        if _bridge_signal_flag.is_set():
            _bridge_signal_flag.clear()
            fire, reason = True, "signal"

        if not fire and (time.monotonic() - last_poll) >= poll_interval:
            last_poll = time.monotonic()
            current = await _read_stamps()
            # Only keys present in BOTH vote for change-detection; a
            # capability appearing/disappearing (opened late, errored this
            # tick) re-baselines silently rather than false-firing.
            changed = [k for k in current if k in baseline and current[k] != baseline[k]]
            baseline = current
            if changed:
                fire, reason = True, f"stamp change: {', '.join(changed)}"

        if fire:
            _logger.info("system.reload (%s)", reason)
            try:
                results = await emit(RELOAD_EVENT, reason=reason)
                _logger.info("system.reload dispatched: %s", results or "no subscribers")
            except EventsError:
                pass  # no kernel right now (mid re-boot) — next trigger retries
            # After a reconcile the stamps have, by definition, been caught
            # up with — re-baseline so the poll doesn't immediately re-fire
            # on the very change we just handled.
            baseline = await _read_stamps()


def _install_signal_handler() -> str:
    """Best-effort SIGUSR1 → flag. Returns how (for the log line):
    'loop' | 'signal' | 'unavailable'. The watcher tick reacts to the flag
    either way, so all three outcomes share one reaction path."""
    if BRIDGE_SIGNAL is None:
        return "unavailable"  # e.g. Windows — poll-only, still fully functional
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(BRIDGE_SIGNAL, _bridge_signal_flag.set)
        return "loop"
    except (NotImplementedError, RuntimeError, ValueError):
        pass
    try:
        _signal.signal(BRIDGE_SIGNAL, lambda signum, frame: _bridge_signal_flag.set())
        return "signal"
    except ValueError:  # not the main thread — poll-only
        return "unavailable"


def install_process_bridge(*, role: str, poll_interval: float = 3.0) -> None:
    """Claim this process as a long-running ARC process: register it in
    .arc/runtime/processes/, install the SIGUSR1 handler (best-effort),
    and start the reload-stamp watcher. Requires a running event loop and
    an active kernel. Idempotent — a second call is a no-op.

    Deliberately NOT called by arc.boot() — see module docstring."""
    global _bridge_task, _bridge_signal_installed, _bridge_pidfile
    if _bridge_task is not None and not _bridge_task.done():
        return
    kernel = _state.get_kernel()
    if kernel is None:
        raise EventsError("install_process_bridge() requires arc.boot() first.")

    how = _install_signal_handler()
    _bridge_signal_installed = how != "unavailable"
    if kernel.project_root is not None:
        _bridge_pidfile = register_process(kernel.project_root, role=role)
    _bridge_task = asyncio.get_running_loop().create_task(_bridge_loop(poll_interval))
    _logger.info(
        "%s ready (pid=%s)",
        role,
        os.getpid(),
        extra={"role": role, "pid": os.getpid(), "signal": how, "poll_interval": poll_interval},
    )


async def uninstall_process_bridge() -> None:
    """Tear the bridge down: cancel the watcher, restore SIGUSR1's default
    disposition, remove this process's registry entry. Safe to call even
    if install never ran."""
    global _bridge_task, _bridge_signal_installed, _bridge_pidfile
    task, _bridge_task = _bridge_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if _bridge_signal_installed and BRIDGE_SIGNAL is not None:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            asyncio.get_running_loop().remove_signal_handler(BRIDGE_SIGNAL)
        with contextlib.suppress(ValueError):
            _signal.signal(BRIDGE_SIGNAL, _signal.SIG_DFL)
        _bridge_signal_installed = False
    if _bridge_pidfile is not None:
        with contextlib.suppress(OSError):
            _bridge_pidfile.unlink(missing_ok=True)
        _bridge_pidfile = None
