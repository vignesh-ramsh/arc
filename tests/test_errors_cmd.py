"""`arc errors` (arc/errorscmd.py): the errors in logs/errors.jsonl, grouped by bug.

The reader is tested against what the writer really produces - arc.errors.capture through the
project's own JSON log format - because the two being one rename apart would pass every test that
writes its own sample lines and show nothing on the day it matters.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arc import cli, errors, errorscmd, log
from arc.settings import SettingsManager

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


def line(fingerprint="aaaaaaaaaaaa", *, minutes_ago=5.0, source="gateway", error_type="ValueError", message="boom", context=None, frames=None) -> str:
    return json.dumps(
        {
            "timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
            "level": "error",
            "logger": "arc.errors",
            "pid": 1,
            "message": f"{error_type}: {message}",
            "error_message": message,
            "source": source,
            "error_type": error_type,
            "fingerprint": fingerprint,
            "frames": frames or ["plugins/shop/api.py:10 in get"],
            "error_context": context or {},
        }
    )


@pytest.fixture
def logs(tmp_path) -> Path:
    directory = tmp_path / "logs"
    directory.mkdir()
    return directory


def write(logs: Path, name: str, *lines: str) -> None:
    (logs / name).write_text("\n".join(lines) + "\n")


def summarise(logs: Path, **kw):
    return errorscmd.summarise(logs, since=kw.pop("since", NOW - timedelta(hours=24)), now=NOW, **kw)


class TestGrouping:
    def test_occurrences_of_one_bug_are_one_problem_with_a_count(self, logs):
        write(logs, "errors.jsonl", *[line("a" * 12, minutes_ago=m) for m in (50, 40, 30)], line("b" * 12))
        problems = summarise(logs)
        assert [(p.fingerprint[:1], p.count) for p in problems] == [("a", 3), ("b", 1)]

    def test_first_and_last_seen_and_the_latest_message(self, logs):
        write(logs, "errors.jsonl", line("a" * 12, minutes_ago=50, message="first"), line("a" * 12, minutes_ago=10, message="latest"), line("a" * 12, minutes_ago=30, message="middle"))
        (problem,) = summarise(logs)
        assert problem.first_seen == NOW - timedelta(minutes=50) and problem.last_seen == NOW - timedelta(minutes=10)
        assert problem.message == "latest"

    def test_the_message_of_a_line_from_an_older_writer_has_the_type_taken_off(self, logs):
        old_style = json.loads(line("a" * 12, message="no error_message field"))
        del old_style["error_message"]
        write(logs, "errors.jsonl", json.dumps(old_style))
        assert summarise(logs)[0].message == "no error_message field"

    def test_the_most_frequent_first_and_among_equals_the_most_recent(self, logs):
        write(logs, "errors.jsonl", line("old1" * 3, minutes_ago=300), line("new1" * 3, minutes_ago=1), line("many" * 3, minutes_ago=200), line("many" * 3, minutes_ago=100))
        assert [p.fingerprint for p in summarise(logs)] == ["many" * 3, "new1" * 3, "old1" * 3]

    def test_only_the_window_counts(self, logs):
        write(logs, "errors.jsonl", line("a" * 12, minutes_ago=30), line("b" * 12, minutes_ago=90))
        assert [p.fingerprint[:1] for p in summarise(logs, since=NOW - timedelta(hours=1))] == ["a"]

    def test_a_source_can_be_asked_for(self, logs):
        write(logs, "errors.jsonl", line("a" * 12, source="gateway"), line("b" * 12, source="lineup"), line("a" * 12, source="lineup"))
        assert {p.fingerprint[:1]: p.count for p in summarise(logs, source="lineup")} == {"a": 1, "b": 1}
        (problem,) = summarise(logs, source="gateway")
        assert problem.count == 1

    def test_one_bug_seen_by_two_kinds_of_process_shows_both(self, logs):
        write(logs, "errors.jsonl", line("a" * 12, source="gateway"), line("a" * 12, source="lineup"))
        (problem,) = summarise(logs)
        assert dict(problem.sources) == {"gateway": 1, "lineup": 1}

    def test_rotated_and_fallback_files_are_read_too(self, logs):
        write(logs, "errors.jsonl", line("a" * 12, minutes_ago=10))
        write(logs, "errors.jsonl.1", line("a" * 12, minutes_ago=100))
        write(logs, "errors.fallback-4242.jsonl", line("a" * 12, minutes_ago=20))
        write(logs, "gateway.jsonl", line("z" * 12))  # a different category: not ours
        (problem,) = summarise(logs)
        assert problem.count == 3

    def test_torn_lines_and_other_log_lines_are_skipped(self, logs):
        write(logs, "errors.jsonl", "not json at all", '{"message": "no fingerprint"}', line("a" * 12), '{"timestamp": "2026-11-07T11', "[1, 2]")
        assert [p.count for p in summarise(logs)] == [1]

    def test_a_dated_line_without_a_fingerprint_is_not_ours(self, logs):
        other = {"timestamp": (NOW - timedelta(minutes=5)).isoformat(), "logger": "arc.errors", "message": "something else"}
        write(logs, "errors.jsonl", json.dumps(other), line("a" * 12))
        assert [p.fingerprint[:1] for p in summarise(logs)] == ["a"]

    def test_a_missing_directory_or_file_is_no_errors_not_a_crash(self, tmp_path):
        assert errorscmd.summarise(tmp_path / "nowhere", since=NOW - timedelta(hours=1)) == []

    def test_a_line_with_no_usable_time_is_ignored(self, logs):
        bad = json.loads(line("a" * 12)); bad["timestamp"] = "yesterday-ish"
        write(logs, "errors.jsonl", json.dumps(bad), line("b" * 12))
        assert [p.fingerprint[:1] for p in summarise(logs)] == ["b"]

    def test_a_time_without_a_zone_is_taken_as_utc(self, logs):
        entry = json.loads(line("a" * 12)); entry["timestamp"] = (NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        write(logs, "errors.jsonl", json.dumps(entry))
        assert len(summarise(logs)) == 1


class TestWhere:
    @pytest.mark.parametrize(
        "context, source, frames, expected",
        [
            ({"route": "/files/upload", "method": "POST"}, "gateway", None, "POST /files/upload"),
            ({"route": "/x"}, "gateway", None, "/x"),
            ({"job": "hrms.payroll", "queue": "mail"}, "lineup", None, "job hrms.payroll (mail)"),
            ({"job": "hrms.payroll"}, "lineup", None, "job hrms.payroll"),
            ({"stage": "process"}, "lineup", None, "lineup process"),
            ({"route": "-", "stage": "identity"}, "gateway", None, "gateway identity"),
            ({}, "cli", ["a.py:1 in f", "b.py:2 in g"], "b.py:2 in g"),
            ({}, "cli", [], "-"),
        ],
    )
    def test_what_to_look_at(self, context, source, frames, expected):
        assert errorscmd._where({"error_context": context, "source": source, "frames": frames}) == expected


class TestTheCommand:
    @pytest.fixture
    def project_logs(self, project: Path, monkeypatch) -> Path:
        monkeypatch.chdir(project)
        (project / "logs").mkdir(exist_ok=True)
        return project / "logs"

    def run(self, *args):
        return CliRunner().invoke(cli.app, ["errors", *args])

    def recent(self, fingerprint="a" * 12, **kw) -> str:
        entry = json.loads(line(fingerprint, **kw))
        entry["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        return json.dumps(entry)

    def test_no_errors_says_so_and_exits_0_even_with_check(self, project_logs):
        result = self.run("--check")
        assert result.exit_code == 0 and "No errors recorded" in result.output

    def test_it_shows_a_table_with_the_counts_and_where(self, project_logs):
        write(project_logs, "errors.jsonl", *[self.recent(context={"route": "/things/{id}", "method": "GET"}, message="bad [bracket]")] * 3)
        result = self.run()
        text = " ".join(result.output.split())
        assert result.exit_code == 0
        assert "3 error(s), 1 distinct problem(s) in the last 24 h" in text and "3 × GET /things/{id}" in text
        assert "ValueError: bad [bracket]" in text  # Rich must not read the brackets as markup
        assert "(gateway)" in text and "id aaaaaaaaaaaa" in text and "ValueError: ValueError" not in text

    def test_brackets_in_a_route_or_a_message_are_printed_as_they_are(self, project_logs):
        write(project_logs, "errors.jsonl", self.recent(context={"route": "/a/[x]/b", "method": "GET"}, message="see [red]this[/red]"))
        text = " ".join(self.run().output.split())
        assert "GET /a/[x]/b" in text and "see [red]this[/red]" in text

    def test_a_very_long_message_is_cut_for_the_screen_but_not_in_json(self, project_logs):
        write(project_logs, "errors.jsonl", self.recent(message="m" * 2000))
        shown = " ".join(self.run().output.split())
        assert "m" * 100 in shown and "m" * 400 not in shown
        assert len(json.loads(self.run("--json").stdout)["problems"][0]["message"]) == 2000

    def test_check_fails_when_there_is_anything(self, project_logs):
        write(project_logs, "errors.jsonl", self.recent())
        assert self.run("--check").exit_code == 1
        assert self.run().exit_code == 0

    def test_check_looks_only_at_the_window(self, project_logs):
        old = json.loads(self.recent()); old["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        write(project_logs, "errors.jsonl", json.dumps(old))
        assert self.run("--hours", "1", "--check").exit_code == 0
        assert self.run("--hours", "4", "--check").exit_code == 1

    def test_json_is_everything_as_one_document(self, project_logs):
        write(project_logs, "errors.jsonl", self.recent("a" * 12), self.recent("b" * 12), self.recent("b" * 12))
        document = json.loads(self.run("--json", "--limit", "1").stdout)
        assert document["errors"] == 3 and [p["fingerprint"][:1] for p in document["problems"]] == ["b", "a"]
        assert document["problems"][0]["count"] == 2 and document["problems"][0]["sources"] == {"gateway": 2}

    def test_the_limit_is_said_when_it_cuts(self, project_logs):
        write(project_logs, "errors.jsonl", self.recent("a" * 12), self.recent("b" * 12), self.recent("c" * 12))
        assert "2 more" in self.run("--limit", "1").output

    def test_outside_a_project_it_says_so(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = self.run()
        assert result.exit_code == 1 and "Not an ARC project" in result.output


class TestWriterAndReaderAgree:
    def test_what_capture_writes_is_what_the_command_reads(self, project: Path, monkeypatch):
        monkeypatch.chdir(project)
        (project / "logs").mkdir(exist_ok=True)
        handler = logging.FileHandler(project / "logs" / "errors.jsonl")
        handler.setFormatter(log.JsonFormatter())
        logger = logging.getLogger("arc.errors")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        errors.reset()
        try:
            def get_user():
                raise KeyError("user 7")

            def get_order():
                raise ValueError("order 9")

            for _ in range(3):
                try:
                    get_user()
                except KeyError as exc:
                    errors.capture(exc, source="gateway", route="/users/{id}", method="GET")
            try:
                get_order()
            except ValueError as exc:
                errors.capture(exc, source="lineup", job="shop.sync", queue="default")
            handler.flush()
            result = CliRunner().invoke(cli.app, ["errors", "--json"])
        finally:
            logger.removeHandler(handler)
            handler.close()
            errors.reset()
        document = json.loads(result.stdout)
        assert document["errors"] == 4
        by_error = {p["error_type"]: p for p in document["problems"]}
        assert by_error["KeyError"]["count"] == 3 and by_error["KeyError"]["where"] == "GET /users/{id}"
        assert by_error["ValueError"]["where"] == "job shop.sync (default)" and by_error["ValueError"]["sources"] == {"lineup": 1}
        assert len({p["fingerprint"] for p in document["problems"]}) == 2
