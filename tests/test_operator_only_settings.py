"""`declare(..., operator_only=True)`: a key the web side may not write.

Such a key decides what ARC executes or which files it can reach (`restart_command`, the filer's
storage root). The guard is in SettingsManager.set/delete itself, so no caller has to remember it:
anything that does not say it is the operator (`operator=True`, which `arc settings set` does) is
refused with OperatorOnlyError - a SettingsError, so existing `except SettingsError` handlers still
catch it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arc.settings import OperatorOnlyError, SettingsError, SettingsManager


@pytest.fixture
def mgr(project: Path) -> SettingsManager:
    manager = SettingsManager(project / ".arc")
    manager.declare("dangerous", doc="Runs things.", operator_only=True)
    manager.declare("harmless", doc="Just a number.", type=int, default=3)
    return manager


class TestTheGuard:
    def test_a_write_without_the_operator_flag_is_refused_and_nothing_is_stored(self, mgr):
        with pytest.raises(OperatorOnlyError, match="arc settings set dangerous"):
            mgr.set("dangerous", "rm -rf /")
        assert mgr.get("dangerous") is None

    def test_the_operator_may_write_and_clear_it(self, mgr):
        mgr.set("dangerous", "ok", operator=True)
        assert mgr.get("dangerous") == "ok"
        assert mgr.delete("dangerous", operator=True) is True
        assert mgr.get("dangerous") is None

    def test_it_cannot_be_cleared_without_the_flag_either(self, mgr):
        mgr.set("dangerous", "ok", operator=True)
        with pytest.raises(OperatorOnlyError):
            mgr.delete("dangerous")
        assert mgr.get("dangerous") == "ok"

    def test_it_is_a_settings_error_so_existing_handlers_still_catch_it(self):
        assert issubclass(OperatorOnlyError, SettingsError)

    def test_the_check_comes_before_any_other_validation(self, mgr):
        """No type or description probing through a refused key."""
        with pytest.raises(OperatorOnlyError):
            mgr.set("dangerous", "x", type="int", description="Mine.")

    def test_an_ordinary_key_needs_no_flag(self, mgr):
        mgr.set("harmless", "7")
        assert mgr.get("harmless") == 7

    def test_a_key_nobody_declared_is_not_restricted(self, mgr):
        mgr.set("added_by_hand", "x", type="str", description="Mine.")
        assert mgr.get("added_by_hand") == "x"


class TestReporting:
    def test_is_operator_only(self, mgr):
        assert mgr.is_operator_only("dangerous") is True
        assert mgr.is_operator_only("harmless") is False
        assert mgr.is_operator_only("never_declared") is False

    def test_list_all_says_so_for_every_key(self, mgr):
        listed = mgr.list_all()
        assert listed["dangerous"]["operator_only"] is True
        assert listed["harmless"]["operator_only"] is False

    def test_the_description_stays_editable(self, mgr):
        """Only the value is guarded: explaining what a key is for is not dangerous."""
        mgr.set_description("dangerous", "Explained better.", by="alice@example.com")
        assert mgr.list_all()["dangerous"]["doc"] == "Explained better."


class TestAFreshManagerHasNoSpecs:
    def test_the_command_line_manager_is_not_blocked_by_specs_it_never_saw(self, project: Path):
        """`arc settings set` and `arc deploy` build a plain manager (no boot, so no declare()); they
        are the operator and pass operator=True anyway."""
        plain = SettingsManager(project / ".arc")
        plain.set("restart_command", "systemctl restart x")
        assert plain.get("restart_command") == "systemctl restart x"
