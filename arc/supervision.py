"""
arc.supervision
------------------
What `arc run` does when one of its children dies, as pure logic with the
process plumbing injected, so it can be tested without spawning anything.

The old behaviour: ANY child exiting unexpectedly stopped every other child.
One background-job worker killed by the OOM killer therefore took the website
down with it. Now a crashed child is restarted on its own, after a short and
growing delay; the rest keep running. Only a child that is crash-looping
(``max_crashes`` unexpected exits within ``window`` seconds) makes the
supervisor give up — restarting it forever would hide a real fault and burn CPU.

Restarting also kills whatever the dead child left behind. Each child runs in
its own session, so its pid is its process-group id; a gateway master that was
SIGKILLed leaves its Granian workers alive, still holding the listening port,
and the replacement could never bind it.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol

#: Delay before the Nth consecutive restart inside the window (the last one repeats).
DEFAULT_DELAYS = (1.0, 2.0, 4.0, 8.0)
DEFAULT_MAX_CRASHES = 5
DEFAULT_WINDOW_SECONDS = 60.0


class Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...


class RestartPolicy:
    """Per-role crash accounting. ``on_exit`` returns the delay in seconds
    before the child should be restarted, or None if it has crashed
    ``max_crashes`` times within ``window`` seconds and should be given up on."""

    def __init__(
        self,
        *,
        max_crashes: int = DEFAULT_MAX_CRASHES,
        window: float = DEFAULT_WINDOW_SECONDS,
        delays: tuple[float, ...] = DEFAULT_DELAYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_crashes = max_crashes
        self._window = window
        self._delays = delays
        self._clock = clock
        self._crashes: dict[str, deque[float]] = {}

    def on_exit(self, role: str) -> float | None:
        now = self._clock()
        crashes = self._crashes.setdefault(role, deque())
        crashes.append(now)
        while crashes and now - crashes[0] > self._window:
            crashes.popleft()
        if len(crashes) >= self._max_crashes:
            return None
        return self._delays[min(len(crashes), len(self._delays)) - 1]

    def recent_crashes(self, role: str) -> int:
        now = self._clock()
        return sum(1 for t in self._crashes.get(role, ()) if now - t <= self._window)

    @property
    def window(self) -> float:
        return self._window

    @property
    def max_crashes(self) -> int:
        return self._max_crashes


@dataclass
class _Child:
    role: str
    argv: list[str]
    proc: Process
    restart_at: float | None = None  # set while waiting out the restart delay
    last_code: int | None = None
    gave_up: bool = False  # crash-looping: reported once, never restarted or counted again


@dataclass
class Supervisor:
    """Owns a set of named children. ``tick()`` is called repeatedly by the
    run loop; it notices exits and drives restarts. Never blocks."""

    spawn: Callable[[str, list[str]], Process]
    kill_group: Callable[[Process], None]
    policy: RestartPolicy = field(default_factory=RestartPolicy)
    clock: Callable[[], float] = time.monotonic
    #: event(kind, role, **info) — kinds: "exited", "restarting", "restarted", "gave_up"
    on_event: Callable[..., None] = lambda *a, **k: None
    #: called after the set of live pids changed (a restart), e.g. to rewrite children.json
    on_change: Callable[[], None] = lambda: None
    _children: dict[str, _Child] = field(default_factory=dict)

    def add(self, role: str, argv: list[str]) -> Process:
        proc = self.spawn(role, argv)
        self._children[role] = _Child(role, argv, proc)
        return proc

    def processes(self) -> list[tuple[str, Process]]:
        return [(c.role, c.proc) for c in self._children.values()]

    def tick(self) -> tuple[str, int] | None:
        """Returns (role, exit_code) when a child is crash-looping and the
        supervisor must stop everything; otherwise None."""
        now = self.clock()
        for child in list(self._children.values()):
            if child.gave_up:
                continue
            if child.restart_at is not None:
                if now >= child.restart_at:
                    self._restart(child)
                continue
            code = child.proc.poll()
            if code is None:
                continue
            child.last_code = code
            self.on_event("exited", child.role, code=code)
            delay = self.policy.on_exit(child.role)
            if delay is None:
                child.gave_up = True
                self.on_event(
                    "gave_up",
                    child.role,
                    code=code,
                    crashes=self.policy.max_crashes,
                    window=self.policy.window,
                )
                return child.role, code or 1
            child.restart_at = now + delay
            self.on_event("restarting", child.role, code=code, delay=delay)
        return None

    def _restart(self, child: _Child) -> None:
        self.kill_group(child.proc)  # whatever the dead child left running (e.g. Granian's workers)
        child.proc = self.spawn(child.role, child.argv)
        child.restart_at = None
        self.on_event("restarted", child.role, pid=child.proc.pid)
        self.on_change()
