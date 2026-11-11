"""A setting records WHO last changed it and WHEN (`setting.updated_by` / `updated_at`, and the same two
columns on `secret`). Every path that writes one says who: the admin console passes the signed-in user's
e-mail, a terminal command passes `cli:<os user>`. (Values are not kept: the store holds the current one.)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

import arc
from arc import cli
from arc.settings import SettingsManager, cli_actor


def who_when(project: Path, key: str, table: str = "setting") -> tuple:
    conn = sqlite3.connect(project / ".arc" / "arc.store.db")
    try:
        return conn.execute(f"SELECT updated_by, updated_at FROM {table} WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _no_kernel_left_booted():
    yield
    arc.shutdown()


class TestTheActorOfATerminalCommand:
    def test_it_is_cli_and_the_os_user(self, monkeypatch):
        monkeypatch.setenv("USER", "vignesh")
        assert cli_actor() == "cli:vignesh"

    def test_without_a_user_it_still_says_something(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        assert cli_actor() == "cli:unknown"


class TestEveryTerminalPathRecordsIt:
    @pytest.fixture(autouse=True)
    def _as_vignesh(self, project, monkeypatch):
        monkeypatch.setenv("USER", "vignesh")
        monkeypatch.chdir(project)

    def test_arc_settings_set(self, project):
        assert CliRunner().invoke(cli.app, ["settings", "set", "k", "v"]).exit_code == 0
        by, at = who_when(project, "k")
        assert by == "cli:vignesh" and at

    def test_arc_settings_set_secret(self, project):
        assert CliRunner().invoke(cli.app, ["settings", "set", "s", "v", "--secret"]).exit_code == 0
        assert who_when(project, "s", "secret")[0] == "cli:vignesh"

    def test_arc_set_maintenance(self, project):
        assert CliRunner().invoke(cli.app, ["set-maintenance", "on"]).exit_code == 0
        assert who_when(project, "gateway_maintenance_mode")[0] == "cli:vignesh"


class TestAWebChangeRecordsTheSignedInUser:
    def test_a_later_write_replaces_who_and_when(self, project):
        mgr = SettingsManager(project / ".arc")
        mgr.set("k", "1", updated_by="alice@example.com")
        first = who_when(project, "k")
        mgr.set("k", "2", updated_by="bob@example.com")
        second = who_when(project, "k")
        assert first[0] == "alice@example.com" and second[0] == "bob@example.com"
        assert second[1] >= first[1]
