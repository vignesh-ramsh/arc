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

import shlex
import sys
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


def _python(code: str) -> str:
    """A restart_command that runs `code` with this interpreter. The command is executed directly
    (no shell), so a test that needs a side effect or an exit code writes it in Python."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


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
        _configure(project, _python("import os; open('ran_here.txt', 'w').write(os.getcwd())"))
        assert cli._run_restart_command(project, required=False) == 0
        assert Path(project / "ran_here.txt").read_text().strip() == str(project.resolve())

    def test_a_failing_command_still_fails_even_when_not_required(self, project: Path, capsys):
        """The point of the whole `required` flag: it softens ONLY the
        never-configured case. A restart that was set up and then breaks is
        a real failure a build must not hide."""
        _configure(project, _python("import sys; sys.exit(7)"))
        assert cli._run_restart_command(project, required=False) == 7
        assert "exited with code 7" in capsys.readouterr().err

    def test_a_failing_command_fails_an_explicit_restart_too(self, project: Path):
        _configure(project, _python("import sys; sys.exit(3)"))
        assert cli._run_restart_command(project) == 3


class TestNoShell:
    """`restart_command` is a setting, and a setting can be written by more hands than the person
    with a shell on the server. It is run as a program with arguments, never through a shell."""

    @pytest.mark.parametrize(
        "command",
        [
            "true && touch pwned",
            "true; touch pwned",
            "true | cat",
            "true > pwned",
            "echo $(touch pwned)",
            "echo `touch pwned`",
            "true & touch pwned",
        ],
    )
    def test_shell_syntax_is_refused_and_nothing_runs(self, project: Path, capsys, command):
        _configure(project, command)
        assert cli._run_restart_command(project) == 1
        captured = capsys.readouterr()
        assert "shell syntax" in captured.err.replace("\n", " ") or "shell syntax" in " ".join(captured.err.split())
        assert not (project / "pwned").exists()

    def test_the_refusal_points_at_a_script(self, project: Path, capsys):
        _configure(project, "a && b")
        cli._run_restart_command(project)
        assert "script" in " ".join(capsys.readouterr().err.split())

    def test_a_quoted_operator_is_just_an_argument(self, project: Path):
        _configure(
            project, _python("import sys; open('args.txt', 'w').write(repr(sys.argv[1:]))") + " 'a|b' '&&'"
        )
        # '&&' on its own is shell syntax even when quoted: refused rather than guessed at
        assert cli._run_restart_command(project) == 1
        _configure(project, _python("import sys; open('args.txt', 'w').write(repr(sys.argv[1:]))") + " 'a|b;c'")
        assert cli._run_restart_command(project) == 0
        assert (project / "args.txt").read_text() == "['a|b;c']"

    def test_nothing_is_expanded(self, project: Path, monkeypatch):
        monkeypatch.setenv("ARC_TEST_SECRET", "leaked")
        _configure(project, _python("import sys; open('args.txt', 'w').write(sys.argv[1])") + " '$ARC_TEST_SECRET'")
        assert cli._run_restart_command(project) == 0
        assert (project / "args.txt").read_text() == "$ARC_TEST_SECRET"

    def test_an_unbalanced_quote_is_an_error_not_a_crash(self, project: Path, capsys):
        _configure(project, "systemctl restart 'arc")
        assert cli._run_restart_command(project) == 1
        assert "cannot be run" in capsys.readouterr().err

    def test_a_program_that_does_not_exist_is_a_clear_127(self, project: Path, capsys):
        _configure(project, "arc-no-such-program --now")
        assert cli._run_restart_command(project) == 127
        assert "not found" in capsys.readouterr().err

    def test_what_arc_deploy_writes_is_still_valid(self):
        assert cli._restart_argv("systemctl --user restart arc-shop.target") == [
            "systemctl", "--user", "restart", "arc-shop.target",
        ]
        assert cli._restart_argv("sudo systemctl restart arc-shop.target") == [
            "sudo", "systemctl", "restart", "arc-shop.target",
        ]
