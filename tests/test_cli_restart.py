"""arc/cli.py's _run_restart_command — the one function both `arc restart`
and `arc build`'s automatic restart go through.

The regression this pins: on a fresh dev machine `restart_command` is not
configured, and `arc build` used to end with exit code 1 and "the restart
failed" immediately after a build that had in fact succeeded. An unset
command is "nothing to restart" for a build — but still an error for an
explicit `arc restart`, and a command that IS configured and fails must fail
either way. Tested directly against the helper (no uv/subprocess build
needed), same convention as test_cli_perform.py and test_cli_run_console.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arc import cli
from arc.settings import SettingsManager


@pytest.fixture(autouse=True)
def _no_stub_regeneration(monkeypatch: pytest.MonkeyPatch):
    """A successful restart ends by shelling out to `arc stubs`, which has
    nothing to boot in a bare tmp project — irrelevant to what's tested."""
    monkeypatch.setattr(cli, "_regenerate_stubs_best_effort", lambda root: None)


def _configure(project: Path, command: str) -> None:
    SettingsManager(project / ".arc").set(cli.RESTART_COMMAND_KEY, command)


class TestNotConfigured:
    def test_an_explicit_restart_is_still_an_error(self, project: Path, capsys):
        """`arc restart` with nothing configured asked for something
        impossible — that must keep failing, with guidance."""
        assert cli._run_restart_command(project) == 1
        captured = capsys.readouterr()
        assert "restart_command" in captured.err
        assert "arc settings set restart_command" in captured.out

    def test_build_style_not_required_is_a_zero_exit(self, project: Path):
        assert cli._run_restart_command(project, required=False) == 0

    def test_build_style_says_what_was_skipped_and_what_to_do(self, project: Path, capsys):
        cli._run_restart_command(project, required=False)
        captured = capsys.readouterr()
        # a note, not an error: nothing may land on stderr
        assert captured.err == ""
        assert "Not restarted" in captured.out
        assert "still on the old code" in captured.out
        assert "arc deploy dev" in captured.out


class TestConfigured:
    def test_a_succeeding_command_returns_zero_in_both_modes(self, project: Path):
        _configure(project, "true")
        assert cli._run_restart_command(project) == 0
        assert cli._run_restart_command(project, required=False) == 0

    def test_the_command_really_runs_in_the_project_root(self, project: Path):
        _configure(project, "pwd > ran_here.txt")
        assert cli._run_restart_command(project, required=False) == 0
        assert (project / "ran_here.txt").read_text().strip() == str(project.resolve())

    def test_a_failing_command_still_fails_even_when_not_required(self, project: Path, capsys):
        """The point of the whole `required` flag: it softens ONLY the
        never-configured case. A restart that was set up and then breaks is
        a real failure a build must not hide."""
        _configure(project, "exit 7")
        assert cli._run_restart_command(project, required=False) == 7
        assert "exited with code 7" in capsys.readouterr().err

    def test_a_failing_command_fails_an_explicit_restart_too(self, project: Path):
        _configure(project, "exit 3")
        assert cli._run_restart_command(project) == 3
