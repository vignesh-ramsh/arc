"""`arc settings set KEY VALUE --type T --description TEXT`: what a new key is created with.

--type and --description are given only when a key is created, the same rule as the admin page. The command
boots the project when either is used, because only a booted project knows the keys plugins declare: without it a
declared key nobody has set yet would look new, and the command would re-type or re-describe it. A project that
cannot boot still works, and says what it could not check. Without the options the command is exactly what it was.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

import arc
from arc import cli


@pytest.fixture(autouse=True)
def _no_kernel_left_booted():
    yield
    arc.shutdown()


@pytest.fixture
def run(project: Path, monkeypatch):
    monkeypatch.chdir(project)

    def invoke(*args: str):
        result = CliRunner().invoke(cli.app, ["settings", "set", *args])
        return result, " ".join(result.output.split())

    return invoke


def stored(project: Path, key: str) -> tuple | None:
    conn = sqlite3.connect(project / ".arc" / "arc.store.db")
    try:
        return conn.execute(
            "SELECT value, is_secret, type, description, description_edited, description_by FROM setting WHERE key = ?",
            (key,),
        ).fetchone()
    except sqlite3.OperationalError:  # no store at all yet: nothing was written
        return None
    finally:
        conn.close()


class TestCreatingAKey:
    def test_type_and_description_are_stored_with_the_key(self, run, project):
        result, output = run(
            "retry_limit", "3", "--type", "int", "--description", "How many times to retry."
        )
        assert result.exit_code == 0, output
        assert "Set retry_limit = 3 (int, with a description)" in output
        row = stored(project, "retry_limit")
        assert row[:5] == ("3", 0, "int", "How many times to retry.", 1)
        assert row[5].startswith("cli:")  # who wrote the description, as for the value

    def test_either_one_alone_is_enough(self, run, project):
        assert run("only_type", "7", "--type", "int")[0].exit_code == 0
        assert stored(project, "only_type")[2:4] == ("int", None)
        assert run("only_words", "x", "--description", "Just words.")[0].exit_code == 0
        assert stored(project, "only_words")[2:4] == (None, "Just words.")

    def test_a_secret_gets_them_too_and_the_value_is_not_printed(self, run, project):
        result, output = run(
            "api_pin", "1234", "--secret", "--type", "int", "--description", "The pin."
        )
        assert result.exit_code == 0, output
        assert "1234" not in output and "secret, int, with a description" in output
        assert stored(project, "api_pin")[:4] == (None, 1, "int", "The pin.")

    def test_a_value_that_is_not_of_the_type_is_refused_and_nothing_is_stored(self, run, project):
        result, output = run("retry_limit", "many", "--type", "int", "--description", "How many.")
        assert result.exit_code == 1 and "int" in output
        assert stored(project, "retry_limit") is None

    def test_a_type_that_does_not_exist_is_refused_by_the_command_itself(self, run, project):
        result, output = run("retry_limit", "3", "--type", "decimal")
        assert result.exit_code == 2 and "str" in output and "bool" in output
        assert stored(project, "retry_limit") is None

    def test_an_empty_description_is_refused(self, run, project):
        result, _ = run("retry_limit", "3", "--description", "   ")
        assert result.exit_code == 1 and stored(project, "retry_limit") is None


class TestAKeyThatExists:
    def test_it_cannot_be_given_a_type_or_a_description(self, run, project):
        run("retry_limit", "3", "--type", "int", "--description", "How many times to retry.")
        for extra in (["--type", "str"], ["--description", "Something else."]):
            result, output = run("retry_limit", "4", *extra)
            assert result.exit_code == 1 and "already exists" in output
        assert stored(project, "retry_limit")[:4] == ("3", 0, "int", "How many times to retry.")

    def test_a_key_the_code_declares_counts_even_though_nobody_set_it(self, run, project):
        """log_level is declared by the kernel but has no value here. Only booting the project knows that."""
        result, output = run("log_level", "DEBUG", "--type", "str", "--description", "Mine.")
        assert result.exit_code == 1 and "already exists" in output
        value, _, _, description, edited, _ = stored(project, "log_level")
        assert (value, edited) == (
            None,
            0,
        ) and description != "Mine."  # still the code's own, and still unset

    def test_its_value_can_still_be_changed_the_old_way_and_is_still_checked(self, run, project):
        run("retry_limit", "3", "--type", "int", "--description", "How many times to retry.")
        assert run("retry_limit", "9")[0].exit_code == 0
        assert stored(project, "retry_limit")[0] == "9"
        result, output = run("retry_limit", "nine")
        assert result.exit_code == 1 and "int" in output
        assert stored(project, "retry_limit")[0] == "9"


class TestWithoutTheOptions:
    def test_the_command_is_what_it_was(self, run, project):
        result, output = run("plain_key", "value")
        assert result.exit_code == 0 and "Set plain_key = value" in output and "(" not in output
        assert stored(project, "plain_key")[2:5] == (None, None, 0)

    def test_it_does_not_boot_the_project(self, run, monkeypatch):
        booted = []
        monkeypatch.setattr(arc, "boot", lambda **kwargs: booted.append(kwargs))
        assert run("plain_key", "value")[0].exit_code == 0
        assert booted == []


class TestAProjectThatCannotBoot:
    def test_the_key_is_still_created_and_the_command_says_what_it_could_not_check(
        self, run, project, monkeypatch
    ):
        def fail(**kwargs):
            raise arc.BootError("no database")

        monkeypatch.setattr(arc, "boot", fail)
        result, output = run(
            "retry_limit", "3", "--type", "int", "--description", "How many times to retry."
        )
        assert result.exit_code == 0, output
        assert "did not boot" in output and "no database" in output
        assert stored(project, "retry_limit")[2:4] == ("int", "How many times to retry.")
