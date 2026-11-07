"""arc.errors: error tracking (arc/errors.py).

What matters: an error is always counted and written as one line with a fingerprint that is the same
for the same bug and different for a different one; nothing sensitive is stored or sent (secrets,
e-mail addresses, request data); a broken destination never breaks the code that reported the error;
and the optional Sentry destination is started lazily, with personal data switched off, and fed only
what `capture` was given. The Sentry SDK itself is replaced by a stand-in recording what it was asked
to do (it is not a dependency of ARC): what it does with that on the wire is the SDK's own business,
and `before_send` is tested on an event shaped like the ones it builds.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc import errors, metrics
from arc.settings import SettingsManager

DSN = "https://publickey@o123.ingest.example.com/456"


@pytest.fixture(autouse=True)
def clean():
    errors.reset()
    yield
    for sink in list(errors._sinks):
        errors.remove_sink(sink)
    errors.reset()


@pytest.fixture
def kernel(project: Path) -> SimpleNamespace:
    kernel = SimpleNamespace(settings=SettingsManager(project / ".arc"), project_root=project)
    errors.declare(kernel)
    return kernel


def boom(message: str = "bad thing", *, kind: type[Exception] = ValueError) -> Exception:
    try:
        raise kind(message)
    except Exception as exc:  # noqa: BLE001 - the test wants the live exception
        return exc


def other_place(message: str = "bad thing") -> Exception:
    try:
        raise ValueError(message)
    except Exception as exc:  # noqa: BLE001
        return exc


def lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "arc.errors"]


class TestCapturing:
    def test_it_returns_the_fingerprint_and_counts_the_error_by_source(self, kernel):
        fingerprint = errors.capture(boom(), source="gateway")
        errors.capture(boom(), source="gateway")
        errors.capture(boom(), source="lineup")
        assert fingerprint and len(fingerprint) == 12 and int(fingerprint, 16) >= 0
        assert errors.metrics() == {"total": 3, "by_source_total": {"gateway": 2, "lineup": 1}, "unforwarded_total": 0}

    def test_each_error_is_one_log_line_with_what_is_needed_to_find_it(self, kernel, caplog):
        caplog.set_level(logging.INFO)
        errors.capture(boom("it broke"), source="gateway", route="/things/{id}", request_id="r-1", status=500)
        (record,) = lines(caplog)
        assert record.levelno == logging.ERROR and record.getMessage() == "ValueError: it broke"
        assert (record.source, record.error_type) == ("gateway", "ValueError")
        assert record.error_message == "it broke"  # without the type: `arc errors` shows them apart
        assert record.error_context == {"route": "/things/{id}", "request_id": "r-1", "status": 500}
        assert record.frames and record.frames[-1].endswith("in boom") and record.forwarded is True
        assert len(record.fingerprint) == 12

    def test_the_line_is_written_to_the_errors_log_as_json(self, kernel):
        from arc import log

        assert log.category_for("arc.errors") == "errors"
        text = log.JsonFormatter().format(
            logging.LogRecord("arc.errors", logging.ERROR, __file__, 1, "%s: %s", ("ValueError", "x"), None)
        )
        assert json.loads(text)["logger"] == "arc.errors"

    def test_it_is_not_echoed_to_every_console(self, kernel):
        from arc import log

        # whoever caught the error already logged its traceback; this one-line record is for the file
        record = logging.LogRecord("arc.errors", logging.ERROR, __file__, 1, "x", (), None)
        assert log._FileOnlyFilter().filter(record) is False

    def test_counts_are_added_up_over_every_process_like_the_other_metrics(self, kernel):
        errors.capture(boom(), source="gateway")
        errors.capture(boom(), source="lineup")
        snapshot_a = {"data": {"errors": errors.metrics()}}
        snapshot_b = {"data": {"errors": {"total": 2, "by_source_total": {"gateway": 2}, "unforwarded_total": 1}}}
        text = metrics.format_prometheus(metrics.aggregate([snapshot_a, snapshot_b]))
        assert "# TYPE arc_errors_total counter" in text and "\narc_errors_total 4\n" in text
        assert 'arc_errors_by_source_total{bucket="gateway"} 3' in text and 'arc_errors_by_source_total{bucket="lineup"} 1' in text
        assert "# TYPE arc_errors_by_source_total counter" in text and "arc_errors_unforwarded_total 1" in text

    def test_metrics_collect_includes_the_errors_of_this_process(self, kernel):
        errors.capture(boom(), source="gateway")
        kernel.capabilities = lambda: {}
        assert metrics.collect(kernel)["errors"]["total"] == 1

    def test_threads_can_capture_at_once(self, kernel):
        def work():
            for _ in range(200):
                errors.capture(boom(), source="gateway")

        threads = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors.metrics()["total"] == 1600


class TestTheFingerprint:
    def test_the_same_bug_has_the_same_fingerprint_whatever_data_triggered_it(self, kernel):
        assert errors.capture(boom("user 17 not found"), source="a") == errors.capture(boom("user 99 not found"), source="b")

    def test_a_different_exception_type_is_a_different_bug(self, kernel):
        assert errors.capture(boom(), source="a") != errors.capture(boom(kind=KeyError), source="a")

    def test_a_different_place_is_a_different_bug(self, kernel):
        assert errors.capture(boom(), source="a") != errors.capture(other_place(), source="a")

    def test_the_anchor_is_project_code_not_the_library_that_raised(self, kernel):
        def parse_a(text):
            try:
                json.loads(text)
            except ValueError as exc:
                return exc

        def parse_b(text):
            try:
                json.loads(text)
            except ValueError as exc:
                return exc

        # one library call site, two project callers: two bugs; one caller, different data: one bug
        a1, a2, b = parse_a("{"), parse_a("[1,"), parse_b("{")
        assert errors.capture(a1, source="x") == errors.capture(a2, source="x") != errors.capture(b, source="x")

    def test_an_exception_that_was_never_raised_still_gets_one(self, kernel):
        assert errors.capture(ValueError("never raised"), source="x") == errors.capture(ValueError("different"), source="x")


class TestWhatIsKept:
    def test_a_secret_setting_value_in_a_message_is_replaced(self, kernel, caplog):
        caplog.set_level(logging.INFO)
        kernel.settings.set("smtp_password", "hunter2-hunter2-hunter2", secret=True)
        errors.capture(boom("login failed with hunter2-hunter2-hunter2 for the server"), source="mail")
        (record,) = lines(caplog)
        assert "hunter2" not in record.getMessage() and "***REDACTED***" in record.getMessage()

    def test_a_short_secret_is_left_alone_so_ordinary_words_survive(self, kernel):
        kernel.settings.set("pin", "abc", secret=True)
        assert errors.scrub("the abc of it") == "the abc of it"

    def test_e_mail_addresses_are_masked(self, kernel):
        assert errors.scrub("Key (email)=(ana.lopez+x@example.co.uk) already exists") == "Key (email)=([email]) already exists"

    def test_a_long_message_is_cut(self, kernel):
        text = errors.scrub("x" * 5000)
        assert len(text) == 500 and text.endswith("…")

    def test_context_keeps_small_facts_and_describes_the_rest_by_type(self, kernel, caplog):
        caplog.set_level(logging.INFO)
        errors.capture(
            boom(), source="lineup", queue="mail", attempt=3, retry=False, none=None, body={"password": "x"}, user=object(),
            long="y" * 1000,
        )
        (record,) = lines(caplog)
        context = record.error_context
        assert context["queue"] == "mail" and context["attempt"] == 3 and context["retry"] is False and context["none"] is None
        assert context["body"] == "<dict>" and context["user"] == "<object>"
        assert len(context["long"]) == 200

    def test_a_secret_in_a_context_value_is_replaced_too(self, kernel, caplog):
        caplog.set_level(logging.INFO)
        kernel.settings.set("api_token", "tok-0123456789abcdef", secret=True)
        errors.capture(boom(), source="gateway", detail="called with tok-0123456789abcdef")
        assert "tok-0123456789abcdef" not in lines(caplog)[0].error_context["detail"]

    def test_frames_are_relative_short_and_have_no_variables(self, kernel, project, caplog):
        caplog.set_level(logging.INFO)

        def deep(n):
            if n == 0:
                raise RuntimeError("bottom")
            deep(n - 1)

        try:
            deep(30)
        except RuntimeError as exc:
            errors.capture(exc, source="x")
        (record,) = lines(caplog)
        assert len(record.frames) == 12  # the innermost twelve
        for frame in record.frames:
            assert not frame.startswith("/") and ".py:" in frame and " in " in frame
        assert str(project) not in " ".join(record.frames)

    def test_a_project_file_is_shown_relative_to_the_project_and_a_library_by_its_package(self, kernel, project):
        inside = errors._relative(str(project / "plugins" / "shop" / "api.py"))
        library = errors._relative("/home/me/.venv/lib/python3.12/site-packages/httpx/_client.py")
        elsewhere = errors._relative("/usr/lib/python3.12/json/decoder.py")
        assert (inside, library, elsewhere) == ("plugins/shop/api.py", "httpx/_client.py", "decoder.py")

    def test_an_exception_name_is_qualified_unless_it_is_a_builtin(self, kernel, caplog):
        caplog.set_level(logging.INFO)

        class Custom(Exception):
            pass

        errors.capture(boom(), source="x")
        errors.capture(Custom("c"), source="x")
        names = [r.error_type for r in lines(caplog)]
        assert names[0] == "ValueError" and names[1].endswith("test_errors.TestWhatIsKept.test_an_exception_name_is_qualified_unless_it_is_a_builtin.<locals>.Custom")


class TestItNeverBreaksTheCaller:
    def test_an_exception_whose_message_cannot_be_read(self, kernel):
        class Hostile(Exception):
            def __str__(self):
                raise RuntimeError("no")

        assert errors.capture(Hostile(), source="x")
        assert errors.metrics()["total"] == 1

    def test_something_that_is_not_an_exception(self, kernel):
        assert errors.capture("just text", source="x") is None  # type: ignore[arg-type]
        assert errors.metrics()["total"] == 0

    def test_without_a_kernel_it_still_records(self):
        assert errors.capture(boom(), source="cli")
        assert errors.metrics()["total"] == 1

    def test_a_sink_that_raises_does_not_stop_the_others_or_the_caller(self, kernel, caplog):
        seen = []

        def bad(event):
            raise RuntimeError("down")

        errors.add_sink(bad)
        errors.add_sink(seen.append)
        assert errors.capture(boom(), source="x")
        assert len(seen) == 1
        assert [r for r in caplog.records if "error sink bad failed" in r.getMessage()]

    def test_a_broken_sink_is_reported_once_a_minute_not_once_per_error(self, kernel, caplog):
        def bad(event):
            raise RuntimeError("down")

        errors.add_sink(bad)
        for _ in range(5):
            errors.capture(boom(), source="x")
        assert len([r for r in caplog.records if "error sink bad failed" in r.getMessage()]) == 1

    def test_a_failing_log_handler_does_not_reach_the_caller(self, kernel, monkeypatch):
        monkeypatch.setattr(errors.logger, "error", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        assert errors.capture(boom(), source="x")


class TestSinks:
    def test_a_sink_gets_the_event(self, kernel):
        seen = []
        errors.add_sink(seen.append)
        exc = boom("hello a@b.com")
        errors.capture(exc, source="gateway", route="/x")
        (event,) = seen
        assert event.source == "gateway" and event.error_type == "ValueError" and event.message == "hello [email]"
        assert event.context == {"route": "/x"} and event.exception is exc and event.fingerprint

    def test_adding_twice_registers_once_and_removing_works(self, kernel):
        seen = []
        errors.add_sink(seen.append)
        errors.add_sink(seen.append)
        errors.capture(boom(), source="x")
        assert len(seen) == 1
        errors.remove_sink(seen.append)
        errors.remove_sink(seen.append)  # not registered any more: no error
        errors.capture(boom(), source="x")
        assert len(seen) == 1

    def test_one_bug_cannot_flood_a_destination(self, kernel, caplog):
        caplog.set_level(logging.INFO)
        seen = []
        errors.add_sink(seen.append)
        for _ in range(25):
            errors.capture(boom(), source="x")
        assert len(seen) == 10  # error_tracking_max_per_minute
        assert errors.metrics()["total"] == 25 and errors.metrics()["unforwarded_total"] == 15  # all counted
        assert len(lines(caplog)) == 25  # and all logged
        assert [r.forwarded for r in lines(caplog)] == [True] * 10 + [False] * 15

    def test_the_limit_is_per_bug(self, kernel):
        seen = []
        errors.add_sink(seen.append)
        for _ in range(15):
            errors.capture(boom(), source="x")
            errors.capture(other_place(), source="x")
        assert len(seen) == 20

    def test_it_is_a_minute_not_forever(self, kernel, monkeypatch):
        seen = []
        errors.add_sink(seen.append)
        clock = [1_000_000.0]
        monkeypatch.setattr(errors.time, "time", lambda: clock[0])
        for _ in range(10):
            errors.capture(boom(), source="x")
        errors.capture(boom(), source="x")
        assert len(seen) == 10
        clock[0] += 59
        errors.capture(boom(), source="x")
        assert len(seen) == 10
        clock[0] += 2
        errors.capture(boom(), source="x")
        assert len(seen) == 11

    def test_the_limit_is_a_setting_and_zero_means_none(self, kernel):
        seen = []
        errors.add_sink(seen.append)
        kernel.settings.set(errors.MAX_PER_MINUTE_KEY, "3")
        for _ in range(8):
            errors.capture(boom(), source="x")
        assert len(seen) == 3
        kernel.settings.set(errors.MAX_PER_MINUTE_KEY, "0")
        errors.capture(other_place(), source="x")
        for _ in range(50):
            errors.capture(boom("again"), source="x")
        assert len(seen) == 3 + 1 + 50  # no limit: every one of the 51 after the first three went through

    def test_the_table_of_remembered_bugs_is_bounded(self, kernel):
        for i in range(errors._MAX_FINGERPRINTS + 50):
            errors._allow(f"fp-{i}", 1.0, 5)
        assert len(errors._recent) <= errors._MAX_FINGERPRINTS


@contextmanager
def fake_sentry(monkeypatch, *, init_error: Exception | None = None):
    calls = SimpleNamespace(init=[], captured=[], tags=[], fingerprints=[])

    class Scope:
        def set_tag(self, key, value):
            calls.tags.append((key, value))

        fingerprint = None

    @contextmanager
    def new_scope():
        scope = Scope()
        yield scope
        calls.fingerprints.append(scope.fingerprint)

    module = types.ModuleType("sentry_sdk")

    def init(**options):
        if init_error is not None:
            raise init_error
        calls.init.append(options)

    calls.flushed = []
    module.init = init
    module.capture_exception = calls.captured.append
    module.new_scope = new_scope
    module.flush = lambda timeout=None: calls.flushed.append(timeout)
    monkeypatch.setitem(sys.modules, "sentry_sdk", module)
    yield calls


class TestTheTracker:
    def test_without_a_dsn_nothing_is_started_or_imported(self, kernel, monkeypatch):
        with fake_sentry(monkeypatch) as calls:
            errors.capture(boom(), source="gateway")
        assert calls.init == [] and calls.captured == []

    def test_with_a_dsn_the_sdk_is_started_once_with_personal_data_off(self, kernel, monkeypatch):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        kernel.settings.set(errors.ENVIRONMENT_KEY, "staging")
        with fake_sentry(monkeypatch) as calls:
            errors.capture(boom(), source="gateway")
            errors.capture(other_place(), source="gateway")
        (options,) = calls.init
        assert options["dsn"] == DSN and options["environment"] == "staging" and options["release"].startswith("arc@")
        assert options["send_default_pii"] is False and options["include_local_variables"] is False
        assert options["max_request_body_size"] == "never"
        assert options["default_integrations"] is False and options["auto_enabling_integrations"] is False
        assert options["traces_sample_rate"] == 0.0 and options["before_send"] is errors.before_send

    def test_it_is_started_on_the_first_error_not_at_boot(self, kernel, monkeypatch):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        with fake_sentry(monkeypatch) as calls:
            assert calls.init == []
            errors.capture(boom(), source="gateway")
            assert len(calls.init) == 1

    def test_the_environment_defaults_to_production(self, kernel, monkeypatch):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        with fake_sentry(monkeypatch) as calls:
            errors.capture(boom(), source="gateway")
        assert calls.init[0]["environment"] == "production"

    def test_the_exception_is_sent_with_its_source_context_and_our_fingerprint(self, kernel, monkeypatch):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        exc = boom()
        with fake_sentry(monkeypatch) as calls:
            fingerprint = errors.capture(exc, source="lineup", queue="mail", job="send_digest", attempt=2)
        assert calls.captured == [exc]
        assert dict(calls.tags) == {"source": "lineup", "queue": "mail", "job": "send_digest", "attempt": "2"}
        assert calls.fingerprints == [[fingerprint]]

    def test_the_dsn_is_never_in_what_is_logged(self, kernel, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        with fake_sentry(monkeypatch):
            errors.capture(boom(f"cannot reach {DSN}"), source="gateway")
        assert DSN not in lines(caplog)[0].getMessage()

    def test_events_over_the_rate_limit_are_not_sent(self, kernel, monkeypatch):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        with fake_sentry(monkeypatch) as calls:
            for _ in range(30):
                errors.capture(boom(), source="gateway")
        assert len(calls.captured) == 10

    def test_a_dsn_without_the_sdk_installed_loses_nothing_and_says_so_once(self, kernel, monkeypatch, caplog):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # makes `import sentry_sdk` fail
        for _ in range(3):
            assert errors.capture(boom(), source="gateway")
        assert errors.metrics()["total"] == 3
        warned = [r for r in caplog.records if "sentry-sdk package is not installed" in r.getMessage()]
        assert len(warned) == 1

    def test_boot_can_say_so_in_advance(self, kernel, monkeypatch):
        assert errors.sentry_missing_notice(kernel) is None  # no DSN: nothing to say
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        monkeypatch.setitem(sys.modules, "sentry_sdk", None)
        assert "uv add sentry-sdk" in errors.sentry_missing_notice(kernel)
        with fake_sentry(monkeypatch):
            assert errors.sentry_missing_notice(kernel) is None

    def test_a_dsn_the_sdk_rejects_is_reported_not_raised(self, kernel, monkeypatch, caplog):
        kernel.settings.set(errors.DSN_KEY, "not a dsn", secret=True)
        with fake_sentry(monkeypatch, init_error=ValueError("Unsupported scheme")) as calls:
            assert errors.capture(boom(), source="gateway")
            assert errors.capture(boom(), source="gateway")
        assert calls.captured == []
        assert len([r for r in caplog.records if "could not start error tracking" in r.getMessage()]) == 1

    def test_what_is_waiting_is_sent_before_the_process_exits(self, kernel, monkeypatch):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        registered = []
        monkeypatch.setattr(errors.atexit, "register", registered.append)
        with fake_sentry(monkeypatch) as calls:
            errors.flush()  # nothing started yet: nothing to do
            assert calls.flushed == []
            errors.capture(boom(), source="gateway")
            assert registered == [errors.flush]  # at interpreter exit...
            errors.flush(timeout=5)  # ...and on demand, e.g. by a worker that is about to die
            assert calls.flushed == [5]

    def test_a_flush_that_fails_does_not_stop_the_process(self, kernel, monkeypatch):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        with fake_sentry(monkeypatch):
            errors.capture(boom(), source="gateway")
            sys.modules["sentry_sdk"].flush = lambda timeout=None: (_ for _ in ()).throw(OSError("network"))
            errors.flush()  # does not raise

    def test_the_sdk_1x_scope_api_is_used_when_there_is_no_new_scope(self, kernel, monkeypatch):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        with fake_sentry(monkeypatch) as calls:
            module = sys.modules["sentry_sdk"]
            module.push_scope = module.new_scope
            del module.new_scope
            errors.capture(boom(), source="gateway")
        assert len(calls.captured) == 1


class TestWhatLeavesTheMachine:
    def event(self) -> dict:
        return {
            "request": {"url": "https://shop.example/employees/7", "headers": {"Authorization": "Bearer x"}, "cookies": "sid=1"},
            "user": {"id": "u-1", "email": "a@b.com", "ip_address": "1.2.3.4"},
            "extra": {"body": "secret"},
            "breadcrumbs": [{"message": "GET /x"}],
            "modules": {"pkg": "1.0"},
            "contexts": {"os": {"name": "Linux"}},
            "tags": {"source": "gateway"},
            "fingerprint": ["abc"],
            "level": "error",
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": "no user ana@example.com",
                        "stacktrace": {
                            "frames": [
                                {
                                    "abs_path": "/srv/shop/plugins/shop/api.py",
                                    "lineno": 10,
                                    "function": "get",
                                    "vars": {"password": "x"},
                                    "pre_context": ["a = 1"],
                                    "context_line": "raise ValueError(...)",
                                    "post_context": ["b = 2"],
                                }
                            ]
                        },
                    }
                ]
            },
        }

    def test_request_user_and_extras_are_dropped(self, kernel):
        cleaned = errors.before_send(self.event())
        for key in ("request", "user", "extra", "breadcrumbs", "modules", "contexts"):
            assert key not in cleaned

    def test_what_identifies_the_bug_is_kept(self, kernel):
        cleaned = errors.before_send(self.event())
        assert cleaned["tags"] == {"source": "gateway"} and cleaned["fingerprint"] == ["abc"] and cleaned["level"] == "error"
        frame = cleaned["exception"]["values"][0]["stacktrace"]["frames"][0]
        assert frame["lineno"] == 10 and frame["function"] == "get"

    def test_variables_and_source_lines_are_dropped_and_paths_made_relative(self, kernel, project):
        event = self.event()
        event["exception"]["values"][0]["stacktrace"]["frames"][0]["abs_path"] = str(project / "plugins" / "shop" / "api.py")
        frame = errors.before_send(event)["exception"]["values"][0]["stacktrace"]["frames"][0]
        for key in ("vars", "pre_context", "context_line", "post_context"):
            assert key not in frame
        assert frame["abs_path"] == "plugins/shop/api.py"

    def test_messages_are_scrubbed(self, kernel):
        kernel.settings.set("api_token", "tok-0123456789abcdef", secret=True)
        event = self.event()
        event["exception"]["values"][0]["value"] = "bad tok-0123456789abcdef for ana@example.com"
        event["message"] = "also tok-0123456789abcdef"
        cleaned = errors.before_send(event)
        assert cleaned["exception"]["values"][0]["value"] == "bad ***REDACTED*** for [email]"
        assert cleaned["message"] == "also ***REDACTED***"

    def test_an_event_without_those_parts_is_fine(self, kernel):
        assert errors.before_send({"level": "error"}) == {"level": "error"}


class TestSettings:
    def test_they_are_declared_with_their_defaults(self, kernel):
        assert kernel.settings.get(errors.DSN_KEY) in (None, "")
        assert kernel.settings.get(errors.ENVIRONMENT_KEY) == "production"
        assert kernel.settings.get(errors.MAX_PER_MINUTE_KEY) == 10

    def test_the_dsn_is_a_secret(self, kernel):
        kernel.settings.set(errors.DSN_KEY, DSN, secret=True)
        assert kernel.settings.get(errors.DSN_KEY) != DSN  # masked unless revealed
        assert kernel.settings.get(errors.DSN_KEY, reveal=True) == DSN
