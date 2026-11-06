"""arc.supervision — what `arc run` does when one of its children dies.

The behaviour this replaces: any child exiting unexpectedly stopped every other
child, so one background-job worker killed by the OOM killer took the website down
(measured: kill -9 one lineup worker, site down 0.5 s later and it stayed down).
Now only the dead child is restarted, with a growing delay, and the supervisor
gives up only on a crash loop. Real processes, real timing and the live outage test
are covered by hand against a running stack; this pins the decisions.
"""

from __future__ import annotations

import pytest

from arc.supervision import RestartPolicy, Supervisor


class FakeProc:
    _next_pid = 1000

    def __init__(self) -> None:
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def die(self, code: int = -9) -> None:
        self.code = code


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


class Harness:
    def __init__(self, clock: Clock, **policy_kwargs) -> None:
        self.clock = clock
        self.spawned: list[tuple[str, FakeProc]] = []
        self.killed: list[FakeProc] = []
        self.events: list[tuple[str, str, dict]] = []
        self.changes = 0
        self.order: list[str] = []
        self.supervisor = Supervisor(
            spawn=self._spawn,
            kill_group=self._kill,
            policy=RestartPolicy(clock=clock, **policy_kwargs),
            clock=clock,
            on_event=lambda kind, role, **info: self.events.append((kind, role, info)),
            on_change=self._changed,
        )

    def _spawn(self, role: str, argv: list[str]) -> FakeProc:
        proc = FakeProc()
        self.spawned.append((role, proc))
        self.order.append(f"spawn:{role}")
        return proc

    def _kill(self, proc: FakeProc) -> None:
        self.killed.append(proc)
        self.order.append(f"kill:{proc.pid}")

    def _changed(self) -> None:
        self.changes += 1

    def proc(self, role: str) -> FakeProc:
        return dict(self.supervisor.processes())[role]

    def kinds(self) -> list[str]:
        return [kind for kind, _, _ in self.events]


class TestRestartPolicy:
    def test_the_first_crash_is_restarted_after_one_second(self, clock):
        assert RestartPolicy(clock=clock).on_exit("worker") == 1.0

    def test_delays_grow_with_each_crash_inside_the_window(self, clock):
        policy = RestartPolicy(clock=clock, max_crashes=10)
        delays = []
        for _ in range(6):
            delays.append(policy.on_exit("worker"))
            clock.advance(0.5)
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]  # the last delay repeats

    def test_the_fifth_crash_within_the_window_is_a_crash_loop(self, clock):
        policy = RestartPolicy(clock=clock)
        results = [policy.on_exit("worker") for _ in range(5)]
        assert results[:4] == [1.0, 2.0, 4.0, 8.0]
        assert results[4] is None

    def test_old_crashes_are_forgotten_once_the_window_has_passed(self, clock):
        policy = RestartPolicy(clock=clock)
        for _ in range(4):
            policy.on_exit("worker")
        clock.advance(61)  # it ran fine for over a minute
        assert policy.on_exit("worker") == 1.0
        assert policy.recent_crashes("worker") == 1

    def test_roles_are_counted_separately(self, clock):
        policy = RestartPolicy(clock=clock)
        for _ in range(4):
            policy.on_exit("lineup-worker-1")
        assert policy.on_exit("gateway") == 1.0  # the gateway has a clean record


class TestSupervisorKeepsTheOthersRunning:
    def test_a_healthy_set_is_left_alone(self, clock):
        h = Harness(clock)
        h.supervisor.add("gateway", ["gw"])
        h.supervisor.add("lineup-worker-1", ["w"])
        for _ in range(5):
            clock.advance(10)
            assert h.supervisor.tick() is None
        assert len(h.spawned) == 2 and h.events == []

    def test_a_crashed_child_is_restarted_and_the_others_are_never_touched(self, clock):
        h = Harness(clock)
        h.supervisor.add("gateway", ["gw"])
        h.supervisor.add("lineup-worker-1", ["w"])
        gateway, worker = h.proc("gateway"), h.proc("lineup-worker-1")

        worker.die(-9)  # the OOM killer takes a job worker
        assert h.supervisor.tick() is None  # noticed, not yet restarted
        assert h.kinds() == ["exited", "restarting"]
        assert len(h.spawned) == 2

        clock.advance(1.0)
        assert h.supervisor.tick() is None
        assert [role for role, _ in h.spawned] == ["gateway", "lineup-worker-1", "lineup-worker-1"]
        assert h.proc("gateway") is gateway and gateway.poll() is None  # untouched
        assert h.proc("lineup-worker-1") is not worker  # replaced
        assert h.kinds()[-1] == "restarted"

    def test_nothing_is_restarted_before_the_delay_has_passed(self, clock):
        h = Harness(clock)
        h.supervisor.add("worker", ["w"])
        h.proc("worker").die()
        h.supervisor.tick()
        clock.advance(0.9)
        h.supervisor.tick()
        assert len(h.spawned) == 1
        clock.advance(0.1)
        h.supervisor.tick()
        assert len(h.spawned) == 2

    def test_what_the_dead_child_left_behind_is_killed_before_the_replacement_starts(self, clock):
        """A SIGKILLed gateway master leaves its Granian workers holding the port; the
        replacement could never bind it."""
        h = Harness(clock)
        h.supervisor.add("gateway", ["gw"])
        old = h.proc("gateway")
        old.die(-9)
        h.supervisor.tick()
        clock.advance(1.0)
        h.supervisor.tick()
        assert h.order.index(f"kill:{old.pid}") < len(h.order) - 1  # killed...
        assert h.order[-1] == "spawn:gateway"  # ...then replaced, in that order
        assert h.killed == [old]

    def test_a_clean_exit_is_also_an_unexpected_exit(self, clock):
        h = Harness(clock)
        h.supervisor.add("worker", ["w"])
        h.proc("worker").die(0)
        h.supervisor.tick()
        assert h.kinds() == ["exited", "restarting"]

    def test_the_children_file_is_refreshed_after_a_restart(self, clock):
        h = Harness(clock)
        h.supervisor.add("worker", ["w"])
        h.proc("worker").die()
        h.supervisor.tick()
        assert h.changes == 0
        clock.advance(1.0)
        h.supervisor.tick()
        assert h.changes == 1

    def test_two_children_dying_together_are_restarted_independently(self, clock):
        h = Harness(clock)
        for role in ("a", "b", "c"):
            h.supervisor.add(role, [role])
        h.proc("a").die()
        h.proc("c").die()
        h.supervisor.tick()
        clock.advance(1.0)
        h.supervisor.tick()
        assert sorted(role for role, _ in h.spawned[3:]) == ["a", "c"]


class TestSupervisorGivesUpOnlyOnACrashLoop:
    def crash_and_restart(self, h: Harness, role: str, clock: Clock):
        h.proc(role).die(-11)
        result = h.supervisor.tick()
        clock.advance(10)  # longer than any delay
        h.supervisor.tick()
        return result

    def test_repeated_crashes_end_with_the_supervisor_giving_up(self, clock):
        h = Harness(clock)
        h.supervisor.add("lineup-worker-1", ["w"])
        results = [self.crash_and_restart(h, "lineup-worker-1", clock) for _ in range(5)]
        assert results[:4] == [None] * 4
        assert results[4] == ("lineup-worker-1", -11)
        assert "gave_up" in h.kinds()

    def test_the_give_up_reports_the_numbers_for_the_message(self, clock):
        h = Harness(clock)
        h.supervisor.add("w", ["w"])
        for _ in range(5):
            self.crash_and_restart(h, "w", clock)
        (info,) = [info for kind, _, info in h.events if kind == "gave_up"]
        assert info["crashes"] == 5 and info["window"] == 60.0 and info["code"] == -11

    def test_a_zero_exit_code_in_a_crash_loop_still_reports_a_failure_code(self, clock):
        h = Harness(clock)
        h.supervisor.add("w", ["w"])
        outcome = None
        for _ in range(5):
            h.proc("w").die(0)
            outcome = h.supervisor.tick()
            clock.advance(10)
            h.supervisor.tick()
        assert outcome == ("w", 1)  # exit non-zero so a supervisor above notices

    def test_giving_up_is_reported_once_even_if_the_loop_keeps_ticking(self, clock):
        h = Harness(clock)
        h.supervisor.add("w", ["w"])
        for _ in range(5):
            self.crash_and_restart(h, "w", clock)
        for _ in range(3):
            clock.advance(10)
            assert h.supervisor.tick() is None
        assert h.kinds().count("gave_up") == 1
        assert len(h.spawned) == 5  # the original plus four restarts; none after giving up

    def test_a_child_that_recovers_is_not_penalised_forever(self, clock):
        h = Harness(clock)
        h.supervisor.add("w", ["w"])
        for _ in range(3):
            self.crash_and_restart(h, "w", clock)
        clock.advance(120)  # stable for two minutes
        assert self.crash_and_restart(h, "w", clock) is None
        assert "gave_up" not in h.kinds()
