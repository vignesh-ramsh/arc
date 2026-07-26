"""arc.events.stats() — the per-Kernel failure-counter mechanism behind
`arc doctor --deep`'s design tension (doctor.py's own module docstring):
a CLI process can never see this, only a live one can. These tests exercise
the counter itself directly against the process-local event bus, with fake
handlers rather than any real plugin — same "kernel-level, duck-typed"
posture as test_run_async.py."""

from __future__ import annotations

from pathlib import Path

import pytest

import arc
from arc.events import EventsError

from .conftest import FakeEntryPoint, write_lock


@pytest.fixture
def booted_project(project: Path):
    write_lock(project, [])
    arc.boot(project_root=project, entry_points=[FakeEntryPoint("noop", lambda kernel: None)])
    return project


class TestStatsRequiresBoot:
    def test_stats_before_boot_raises(self):
        with pytest.raises(EventsError):
            arc.events.stats()


class TestStatsTracksOutcomes:
    async def test_a_never_emitted_event_has_no_entry(self, booted_project):
        async def handler(**kw):
            pass

        arc.events.on("some.event", handler)
        assert arc.events.stats() == {}

    async def test_successful_handlers_count_as_ok(self, booted_project):
        async def handler(**kw):
            pass

        arc.events.on("thing.happened", handler)
        await arc.events.emit("thing.happened")
        await arc.events.emit("thing.happened")

        stats = arc.events.stats()
        counters = stats["thing.happened"]["<direct>.handler"]
        assert counters == {"ok": 2, "error": 0, "last_error": None, "last_error_at": None}

    async def test_raising_handlers_count_as_error_without_stopping_dispatch(self, booted_project):
        calls: list[str] = []

        async def bad(**kw):
            raise ValueError("boom")

        async def good(**kw):
            calls.append("good ran")

        arc.events.on("thing.happened", bad)
        arc.events.on("thing.happened", good)
        results = await arc.events.emit("thing.happened")

        # the raising handler never stops the next one from running (module
        # docstring's own handler-semantics contract)
        assert calls == ["good ran"]
        assert results["<direct>.good"] == "ok"

        stats = arc.events.stats()
        bad_counters = stats["thing.happened"]["<direct>.bad"]
        assert bad_counters["ok"] == 0
        assert bad_counters["error"] == 1
        assert "ValueError: boom" in bad_counters["last_error"]
        assert bad_counters["last_error_at"] is not None

        good_counters = stats["thing.happened"]["<direct>.good"]
        assert good_counters == {"ok": 1, "error": 0, "last_error": None, "last_error_at": None}

    async def test_counts_accumulate_across_multiple_emits(self, booted_project):
        async def flaky(**kw):
            if flaky.calls % 2 == 0:
                flaky.calls += 1
                raise RuntimeError("every other call fails")
            flaky.calls += 1

        flaky.calls = 0

        arc.events.on("thing.happened", flaky)
        for _ in range(4):
            await arc.events.emit("thing.happened")

        counters = arc.events.stats()["thing.happened"]["<direct>.flaky"]
        assert counters["ok"] == 2
        assert counters["error"] == 2

    async def test_different_events_get_independent_counters(self, booted_project):
        async def handler(**kw):
            pass

        arc.events.on("event.a", handler)
        arc.events.on("event.b", handler)
        await arc.events.emit("event.a")

        stats = arc.events.stats()
        assert stats["event.a"]["<direct>.handler"]["ok"] == 1
        assert "event.b" not in stats


class TestStatsLifetimeMatchesKernel:
    async def test_a_fresh_boot_starts_with_empty_stats(self, booted_project):
        async def handler(**kw):
            pass

        arc.events.on("thing.happened", handler)
        await arc.events.emit("thing.happened")
        assert arc.events.stats() != {}

        arc.runtime.shutdown()
        arc.boot(
            project_root=booted_project,
            entry_points=[FakeEntryPoint("noop", lambda kernel: None)],
        )
        assert arc.events.stats() == {}
