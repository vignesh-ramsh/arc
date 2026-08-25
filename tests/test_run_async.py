"""arc.runtime.run_async — the shared CLI bootstrap helper (§1 P0) that
replaced the hand-rolled boot-then-open dance in authn/lineup/pgdb's own
CLIs. Uses fake capabilities with recorded open()/close() calls rather than
real pgdb/redix — this is a kernel-level test of the sequencing/cleanup
contract, not of any particular plugin's I/O."""

from __future__ import annotations

from pathlib import Path

import pytest

from arc.runtime import run_async

from .conftest import FakeEntryPoint, write_lock


class FakeCapability:
    def __init__(self, name: str, calls: list[str]):
        self._name = name
        self._calls = calls

    async def open(self) -> None:
        self._calls.append(f"open:{self._name}")

    async def close(self) -> None:
        self._calls.append(f"close:{self._name}")


class NoLifecycleCapability:
    """A registered capability with no open()/close() at all — duck-typed,
    same convention Gateway's own lifespan sweep already uses; run_async
    must skip it rather than error."""


def _register_two_capabilities(calls: list[str]):
    def register_a(kernel):
        kernel.export("cap_a", FakeCapability("cap_a", calls))

    def register_b(kernel):
        kernel.export("cap_b", FakeCapability("cap_b", calls))

    return register_a, register_b


@pytest.fixture
def two_capability_project(project: Path):
    calls: list[str] = []
    register_a, register_b = _register_two_capabilities(calls)
    write_lock(project, [{"name": "cap_a"}, {"name": "cap_b"}])
    entry_points = [FakeEntryPoint("cap_a", register_a), FakeEntryPoint("cap_b", register_b)]
    return project, entry_points, calls


class TestRunAsyncOpensAndCloses:
    def test_opens_in_order_and_closes_in_reverse(self, two_capability_project):
        project, entry_points, calls = two_capability_project

        async def _do():
            calls.append("run")
            return "result"

        result = run_async(
            _do(), open=("cap_a", "cap_b"), project_root=project, entry_points=entry_points
        )

        assert result == "result"
        assert calls == ["open:cap_a", "open:cap_b", "run", "close:cap_b", "close:cap_a"]

    def test_skips_a_capability_name_not_registered(self, two_capability_project):
        project, entry_points, calls = two_capability_project

        async def _do():
            return None

        run_async(
            _do(),
            open=("cap_a", "not_a_real_capability"),
            project_root=project,
            entry_points=entry_points,
        )
        assert calls == ["open:cap_a", "close:cap_a"]

    def test_skips_a_capability_with_no_open_close_methods(self, project: Path):
        def register(kernel):
            kernel.export("bare", NoLifecycleCapability())

        write_lock(project, [{"name": "bare"}])

        async def _do():
            return "ok"

        result = run_async(
            _do(),
            open=("bare",),
            project_root=project,
            entry_points=[FakeEntryPoint("bare", register)],
        )
        assert result == "ok"

    def test_does_not_open_a_capability_not_listed(self, two_capability_project):
        project, entry_points, calls = two_capability_project

        async def _do():
            return None

        run_async(_do(), open=("cap_a",), project_root=project, entry_points=entry_points)
        assert calls == ["open:cap_a", "close:cap_a"]


class TestRunAsyncErrorCleanup:
    def test_closes_what_was_opened_even_when_coro_raises(self, two_capability_project):
        project, entry_points, calls = two_capability_project

        async def _boom():
            calls.append("run")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            run_async(
                _boom(), open=("cap_a", "cap_b"), project_root=project, entry_points=entry_points
            )

        assert calls == ["open:cap_a", "open:cap_b", "run", "close:cap_b", "close:cap_a"]
