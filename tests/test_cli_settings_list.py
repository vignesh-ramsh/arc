"""`arc settings list [--secrets]` and `arc settings get KEY --reveal`: secrets are reachable from the CLI only.

`list` shows ordinary settings and never a secret; `list --secrets` shows ONLY secrets, every value masked.
`get KEY --reveal` is the one way to see a secret's value, and each reveal is written to the access log (who, which
key, when) - the admin console no longer reveals anything, so this is the only writer left. The log is written
before the value is printed, and a reveal that cannot be logged shows nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

import arc
from arc import cli
from arc.settings import REDACTED, SettingsError, SettingsManager

SECRET_VALUE = "TOP-SECRET-VALUE-0451"


@pytest.fixture(autouse=True)
def _no_kernel_left_booted():
    yield
    arc.shutdown()


@pytest.fixture
def mgr(project: Path, monkeypatch) -> SettingsManager:
    """A project holding one plain setting and one secret, with the working directory inside it."""
    monkeypatch.chdir(project)
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setattr(cli, "console", Console(width=400))  # a wide table: no key or value wrapped
    manager = SettingsManager(project / ".arc")
    manager.set("plain_one", "hello-plain", description="A plain probe.")
    manager.set("api_token", SECRET_VALUE, secret=True, description="A secret probe.")
    return manager


def run(*args: str):
    result = CliRunner().invoke(cli.app, ["settings", *args])
    return result, result.output


class TestListHidesSecrets:
    def test_a_plain_listing_has_the_settings_and_no_secret(self, mgr):
        result, output = run("list")
        assert result.exit_code == 0, output
        assert "plain_one" in output and "hello-plain" in output
        assert "log_level" in output  # a key the kernel itself declares is a setting like any other
        assert "api_token" not in output  # not even the name
        assert SECRET_VALUE not in output and REDACTED not in output

    def test_the_kind_column_says_setting_for_every_row(self, mgr):
        _, output = run("list")
        assert "secret" not in output.replace("--secrets", "")

    def test_a_declared_but_unset_secret_is_not_listed_either(self, mgr):
        mgr.declare("never_set_token", secret=True)
        _, output = run("list")
        assert "never_set_token" not in output


class TestListSecrets:
    def test_only_secrets_are_listed(self, mgr):
        result, output = run("list", "--secrets")
        assert result.exit_code == 0, output
        assert "api_token" in output
        assert "plain_one" not in output and "hello-plain" not in output
        assert "log_level" not in output  # no ordinary setting at all, declared or set

    def test_the_value_is_masked_and_never_printed(self, mgr):
        _, output = run("list", "--secrets")
        assert REDACTED in output and SECRET_VALUE not in output

    def test_the_description_is_shown_so_the_key_can_be_told_apart(self, mgr):
        _, output = run("list", "--secrets")
        assert "A secret probe." in output

    def test_a_secret_with_no_value_yet_is_listed_too(self, mgr):
        mgr.declare("never_set_token", secret=True)
        _, output = run("list", "--secrets")
        assert "never_set_token" in output and "api_token" in output

    def test_a_declared_default_is_masked_like_the_value(self, mgr, monkeypatch):
        """A placeholder credential written in code is as good as a value; the listing does not print it."""
        mgr.declare("csrf_key", secret=True, default="dev-placeholder-credential")
        monkeypatch.setattr(cli, "_settings_manager_that_knows_declared_keys", lambda root: (mgr, None))
        _, output = run("list", "--secrets")
        assert "csrf_key" in output and "dev-placeholder-credential" not in output

    def test_a_secret_the_kernel_declares_itself_is_listed_like_any_other(self, project, monkeypatch):
        """A bare project already has one (`error_tracking_dsn`), which the plain listing no longer shows."""
        monkeypatch.chdir(project)
        monkeypatch.setattr(cli, "console", Console(width=400))
        assert "error_tracking_dsn" in run("list", "--secrets")[1]
        assert "error_tracking_dsn" not in run("list")[1]

    def test_with_no_secret_at_all_it_says_so(self, mgr, monkeypatch):
        class OnlyPlain:
            def list_all(self):
                return {"plain_one": {"value": "x", "kind": "setting", "type": None, "default": None, "doc": ""}}

        monkeypatch.setattr(cli, "_settings_manager_that_knows_declared_keys", lambda root: (OnlyPlain(), None))
        result, output = run("list", "--secrets")
        assert result.exit_code == 0 and "no secrets declared or set yet" in output
        assert "plain_one" not in output

    def test_a_project_that_does_not_boot_still_lists_them(self, mgr, monkeypatch):
        monkeypatch.setattr(
            cli, "_settings_manager_that_knows_declared_keys", lambda root: (mgr, "pgdb_dsn is not set")
        )
        result, output = run("list", "--secrets")
        assert result.exit_code == 0 and "api_token" in output and "boot failed: pgdb_dsn is not set" in output
        assert "plain_one" not in output and SECRET_VALUE not in output


def test_the_help_names_the_flag_and_says_what_it_does(mgr):
    result, output = run("list", "--help")
    text = " ".join(output.split())
    assert result.exit_code == 0
    assert "--secrets" in text
    assert "ONLY secrets" in text and "not listed at all" in text
    assert "arc settings get" in text and "--reveal" in text


class TestRevealIsRecorded:
    def test_a_masked_read_shows_no_value_and_records_nothing(self, mgr):
        result, output = run("get", "api_token")
        assert result.exit_code == 0 and REDACTED in output and SECRET_VALUE not in output
        assert mgr.access_log() == []

    def test_a_reveal_shows_the_value_and_records_who_and_when(self, mgr):
        result, output = run("get", "api_token", "--reveal")
        assert result.exit_code == 0 and SECRET_VALUE in output
        (row,) = mgr.access_log()
        assert (row["key"], row["accessed_by"]) == ("api_token", "cli:tester")
        assert row["accessed_at"]

    def test_each_reveal_is_its_own_row(self, mgr):
        run("get", "api_token", "--reveal")
        run("get", "api_token", "--reveal")
        assert len(mgr.access_log(key="api_token")) == 2

    def test_the_log_is_read_back_with_access_log(self, mgr):
        run("get", "api_token", "--reveal")
        result, output = run("access-log")
        assert result.exit_code == 0 and "api_token" in output and "cli:tester" in output
        assert SECRET_VALUE not in output

    def test_a_plain_setting_needs_no_reveal_and_records_nothing(self, mgr):
        result, output = run("get", "plain_one", "--reveal")
        assert result.exit_code == 0 and "hello-plain" in output
        assert mgr.access_log() == []

    def test_an_unset_secret_is_an_error_and_records_nothing(self, mgr):
        mgr.declare("never_set_token", secret=True)
        result, _ = run("get", "never_set_token", "--reveal")
        assert result.exit_code == 1
        assert mgr.access_log() == []

    def test_a_reveal_that_cannot_be_recorded_shows_nothing(self, mgr, monkeypatch):
        def refuse(self, key, accessed_by):
            raise SettingsError("the store is read-only")

        monkeypatch.setattr(SettingsManager, "record_secret_access", refuse)
        result, output = run("get", "api_token", "--reveal")
        assert result.exit_code == 1
        assert SECRET_VALUE not in output
        assert "so it was not shown" in " ".join(output.split())
