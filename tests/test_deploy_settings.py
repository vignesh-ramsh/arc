"""The two deployment settings are declared at boot, so they are listed (with a description) on the admin
Settings page wherever they are read; and the log settings show their real defaults there."""

from __future__ import annotations

from pathlib import Path

import pytest

import arc
from arc import deploy_settings

from .conftest import FakeEntryPoint, write_lock


@pytest.fixture
def booted_project(project: Path):
    write_lock(project, [])
    arc.boot(project_root=project, entry_points=[FakeEntryPoint("noop", lambda k: None)])
    return project


class TestDeploymentSettings:
    @pytest.mark.parametrize(
        "key", ["restart_command", "lineup_queues", "gateway_workers", "lineup_workers"]
    )
    def test_they_are_listed_with_a_description_before_anyone_sets_them(self, booted_project, key):
        entry = arc.settings.list_all()[key]
        assert entry["doc"].strip() and entry["value"] is None and entry["kind"] == "setting"

    def test_the_cli_uses_the_same_names(self):
        from arc import cli

        for name in (
            "RESTART_COMMAND_KEY",
            "LINEUP_QUEUES_KEY",
            "GATEWAY_WORKERS_KEY",
            "LINEUP_WORKERS_KEY",
        ):
            assert getattr(cli, name) == getattr(deploy_settings, name)

    def test_a_value_set_earlier_is_kept(self, project: Path):
        write_lock(project, [])
        arc.boot(project_root=project, entry_points=[FakeEntryPoint("noop", lambda k: None)])
        arc.settings.set("restart_command", "systemctl --user restart arc", operator=True)
        arc.shutdown()
        arc.boot(project_root=project, entry_points=[FakeEntryPoint("noop", lambda k: None)])
        assert arc.settings.get("restart_command") == "systemctl --user restart arc"
        assert arc.settings.list_all()["restart_command"]["doc"].strip()


class TestTheyAreChangedOnTheServerOnly:
    """They decide what `arc restart` runs and what `arc deploy` writes into systemd units, so the
    admin console - a web login, even a Superuser's - may not write them."""

    KEYS = ("restart_command", "lineup_queues", "gateway_workers", "lineup_workers")

    @pytest.mark.parametrize("key", KEYS)
    def test_they_are_listed_as_operator_only(self, booted_project, key):
        assert arc.settings.list_all()[key]["operator_only"] is True
        assert arc.settings.is_operator_only(key) is True

    @pytest.mark.parametrize("key", KEYS)
    def test_a_web_request_cannot_set_them(self, booted_project, key):
        with pytest.raises(arc.settings.OperatorOnlyError, match="on the server"):
            arc.settings.set(key, "x")
        assert arc.settings.get(key) is None

    def test_it_is_still_a_settings_error_for_callers_that_catch_that(self, booted_project):
        with pytest.raises(arc.settings.SettingsError):
            arc.settings.set("restart_command", "x")

    def test_nor_clear_them(self, booted_project):
        arc.settings.set("restart_command", "true", operator=True)
        with pytest.raises(arc.settings.OperatorOnlyError):
            arc.settings.delete("restart_command")
        assert arc.settings.get("restart_command") == "true"
        assert arc.settings.delete("restart_command", operator=True) is True

    def test_ordinary_settings_are_not_affected(self, booted_project):
        arc.settings.set("log_level", "DEBUG")
        assert arc.settings.get("log_level") == "DEBUG"
        assert arc.settings.list_all()["log_level"]["operator_only"] is False


class TestLogSettingsShowTheirDefaults:
    def test_level_size_and_copies(self, booted_project):
        data = arc.settings.list_all()
        assert (data["log_level"]["type"], data["log_level"]["default"]) == ("str", "INFO")
        assert (data["log_max_bytes"]["type"], data["log_max_bytes"]["default"]) == (
            "int",
            10 * 1024 * 1024,
        )
        assert (data["log_backup_count"]["type"], data["log_backup_count"]["default"]) == ("int", 5)

    def test_unset_they_still_behave_as_before(self, booted_project):
        from arc import log

        sink = log.read_sink_settings(arc.settings._bound_manager())
        assert (sink.level, sink.max_bytes, sink.backup_count) == ("INFO", 10 * 1024 * 1024, 5)

    def test_a_value_that_is_set_wins(self, project: Path):
        write_lock(project, [])
        arc.boot(project_root=project, entry_points=[FakeEntryPoint("noop", lambda k: None)])
        arc.settings.set("log_max_bytes", "2048")
        arc.settings.set("log_level", "DEBUG")
        from arc import log

        sink = log.read_sink_settings(arc.settings._bound_manager())
        assert (sink.level, sink.max_bytes) == ("DEBUG", 2048)
