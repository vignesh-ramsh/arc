"""arc.expiring.Pruner: stale entries leave on a timer, memory stays bounded, per-call cost stays flat."""

from __future__ import annotations

import time

import pytest

from arc.expiring import Pruner


def _counts(n: int, *, start: float = 0.0) -> dict[str, tuple[int, float]]:
    return {f"k{i}": (1, start) for i in range(n)}


def _stale_before(cutoff: float):
    return lambda value: value[1] < cutoff


class TestWhenItRuns:
    def test_a_small_dictionary_is_never_touched(self):
        mapping = _counts(10)
        pruner = Pruner(threshold=10)

        assert pruner.prune(mapping, lambda v: True, now=100.0) is False
        assert len(mapping) == 10

    def test_above_the_threshold_stale_entries_are_dropped(self):
        mapping = {**_counts(6, start=0.0), **{f"live{i}": (1, 99.0) for i in range(6)}}
        pruner = Pruner(threshold=10)

        assert pruner.prune(mapping, _stale_before(50.0), now=100.0) is True

        assert sorted(mapping) == [f"live{i}" for i in range(6)]
        assert pruner.stale_dropped == 6

    def test_it_runs_at_most_once_per_interval(self):
        mapping = _counts(20)
        pruner = Pruner(threshold=10, interval=1.0)

        assert pruner.prune(mapping, lambda v: False, now=100.0) is True
        assert pruner.prune(mapping, lambda v: True, now=100.5) is False  # too soon: nothing dropped
        assert len(mapping) == 20
        assert pruner.prune(mapping, lambda v: True, now=101.0) is True
        assert mapping == {}

    def test_it_prunes_in_place(self):
        mapping = _counts(20)
        same = mapping
        Pruner(threshold=10).prune(mapping, lambda v: True, now=1.0)
        assert mapping is same


class TestTheHardCap:
    def test_live_entries_beyond_the_cap_are_evicted_oldest_first(self):
        mapping = {f"k{i}": (1, 99.0) for i in range(30)}  # all live
        pruner = Pruner(threshold=10, hard_cap=20)

        pruner.prune(mapping, lambda v: False, now=100.0)

        assert len(mapping) == 20
        assert list(mapping) == [f"k{i}" for i in range(10, 30)]  # k0..k9 (the oldest inserted) went
        assert pruner.evicted == 10 and pruner.stale_dropped == 0

    def test_stale_ones_go_first_so_live_counters_survive_when_possible(self):
        mapping = {**_counts(15, start=0.0), **{f"live{i}": (1, 99.0) for i in range(10)}}
        pruner = Pruner(threshold=10, hard_cap=20)

        pruner.prune(mapping, _stale_before(50.0), now=100.0)

        assert len(mapping) == 10 and all(k.startswith("live") for k in mapping)
        assert pruner.evicted == 0

    def test_the_cap_may_not_be_below_the_threshold(self):
        with pytest.raises(ValueError):
            Pruner(threshold=100, hard_cap=10)


class TestCostDoesNotGrowWithKeys:
    """The reason this exists: the per-request cost above 4096 keys was O(n)."""

    @pytest.mark.parametrize("keys", [5_000, 50_000])
    def test_a_burst_of_calls_costs_one_pass_not_one_per_call(self, keys):
        mapping = _counts(keys, start=100.0)  # all live: nothing to drop, the old code rebuilt anyway
        pruner = Pruner()
        started = time.perf_counter()
        for i in range(1_000):
            pruner.prune(mapping, _stale_before(50.0), now=100.0 + i * 0.0001)  # 1,000 calls inside 0.1 s
        elapsed = time.perf_counter() - started

        # One O(n) pass (a few ms at most) instead of 1,000 of them (about 2.5 s at 50,000 keys).
        assert elapsed < 0.25, f"{elapsed:.3f}s for 1,000 prune() calls at {keys} keys"
        assert len(mapping) == keys
