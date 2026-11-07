"""`arc run`'s worker counts (arc/cli.py `_resolve_worker_count`).

The automatic choice is sized from the machine and never exceeds 8. A number the operator sets is
honoured as given - `arc deploy` already did, `arc run` still clamped it to 8, so a 32-core server
ran 8 web workers whatever it was told. What keeps a large count from overrunning PostgreSQL is the
connection budget, not a clamp.
"""

from __future__ import annotations

import pytest

from arc import cli, sizing


class FakeKernel:
    def __init__(self, **values):
        self.settings = self
        self._values = values

    def get(self, key):
        return self._values.get(key)


@pytest.fixture
def big_machine(monkeypatch):
    monkeypatch.setattr(sizing, "detect_cpu_count", lambda: 64)
    monkeypatch.setattr(sizing, "detect_memory_gb", lambda: 512.0)


def resolve(value, key="gateway_workers"):
    return cli._resolve_worker_count(FakeKernel(**({key: value} if value is not None else {})), key)


class TestAutomatic:
    def test_sized_from_the_machine_but_never_above_the_ceiling(self, big_machine):
        assert resolve(None) == sizing.DEFAULT_CEILING == 8

    def test_a_small_machine_gets_fewer(self, monkeypatch):
        monkeypatch.setattr(sizing, "detect_cpu_count", lambda: 2)
        monkeypatch.setattr(sizing, "detect_memory_gb", lambda: 4.0)
        assert resolve(None) < 8

    def test_an_empty_setting_is_unset(self, big_machine):
        assert resolve("") == 8


class TestExplicit:
    @pytest.mark.parametrize("n", ["1", "3", "8", "9", "16", "32", "100"])
    def test_what_the_operator_set_is_what_runs(self, big_machine, n):
        assert resolve(n) == int(n)

    def test_the_lineup_key_works_the_same_way(self, big_machine):
        assert resolve("24", key="lineup_workers") == 24

    @pytest.mark.parametrize("bad", ["0", "-5"])
    def test_less_than_one_becomes_one_and_says_so(self, big_machine, bad, capsys):
        assert resolve(bad) == 1
        assert "at least 1 worker" in capsys.readouterr().out

    def test_not_a_number_falls_back_to_the_automatic_count_and_says_so(self, big_machine, capsys):
        assert resolve("lots") == 8
        assert "not a valid integer" in capsys.readouterr().err

    def test_a_custom_ceiling_only_limits_the_automatic_choice(self, big_machine):
        kernel = FakeKernel(gateway_workers="12")
        assert cli._resolve_worker_count(kernel, "gateway_workers", ceiling=4) == 12
        assert cli._resolve_worker_count(FakeKernel(), "gateway_workers", ceiling=4) == 4
