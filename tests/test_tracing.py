"""arc.tracing — settings declare()/validate_sample_rate() (boot-time range
check), get_tracer()/start_exporter()/stop_exporter() lifecycle, span()'s
clean no-op when tracing is off, and current_trace_labels()/continue_trace()
round-tripping for background-job span continuation. No real OTLP collector
needed for any of this — construction succeeds without a reachable
endpoint (BatchSpanProcessor buffers/exports asynchronously)."""

from __future__ import annotations

import http.server
import logging
import socket
import threading
import time
from pathlib import Path

import pytest

import arc
from arc import tracing
from arc.runtime import BootError
from arc.tracing import TracingError, continue_trace, current_trace_labels, get_tracer, span, start_exporter, stop_exporter

from .conftest import FakeEntryPoint, write_lock


@pytest.fixture
def booted_project(project: Path):
    write_lock(project, [])
    arc.boot(project_root=project, entry_points=[FakeEntryPoint("noop", lambda k: None)])
    return project


class TestSettingsDeclaredAtBoot:
    def test_endpoint_defaults_to_empty(self, booted_project):
        assert arc.settings.get("tracing_otlp_endpoint") == ""

    def test_sample_rate_defaults_to_100(self, booted_project):
        assert arc.settings.get("tracing_sample_rate_percent") == 100

    def test_declared_keys_show_up_in_list_all(self, booted_project):
        data = arc.settings.list_all()
        assert data["tracing_otlp_endpoint"]["kind"] == "setting"
        assert data["tracing_sample_rate_percent"]["type"] == "int"


class TestSampleRateRangeValidatedAtBoot:
    def test_in_range_value_boots_cleanly(self, project: Path):
        write_lock(project, [])
        mgr_kernel = arc.boot(project_root=project, entry_points=())
        arc.settings.set("tracing_sample_rate_percent", "50")
        # re-boot to re-run validate_sample_rate against the new value
        arc.shutdown()
        arc.boot(project_root=project, entry_points=())  # must not raise

    def test_out_of_range_value_fails_boot(self, project: Path):
        write_lock(project, [])
        arc.boot(project_root=project, entry_points=())
        arc.settings.set("tracing_sample_rate_percent", "150")
        arc.shutdown()
        with pytest.raises(BootError, match="tracing_sample_rate_percent"):
            arc.boot(project_root=project, entry_points=())

    def test_negative_value_fails_boot(self, project: Path):
        write_lock(project, [])
        arc.boot(project_root=project, entry_points=())
        arc.settings.set("tracing_sample_rate_percent", "-1")
        arc.shutdown()
        with pytest.raises(BootError, match="tracing_sample_rate_percent"):
            arc.boot(project_root=project, entry_points=())


class TestExporterLifecycle:
    def test_get_tracer_is_none_before_start(self, booted_project):
        assert get_tracer() is None

    def test_start_before_boot_raises(self):
        with pytest.raises(TracingError):
            start_exporter(role="test-role")

    def test_unset_endpoint_start_is_a_no_op(self, booted_project):
        start_exporter(role="test-role")
        try:
            assert get_tracer() is None
        finally:
            stop_exporter()

    def test_set_endpoint_start_constructs_a_real_tracer(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", "http://localhost:4318/v1/traces")
        start_exporter(role="test-role")
        try:
            assert get_tracer() is not None
        finally:
            stop_exporter()
        assert get_tracer() is None  # stop_exporter() tears it back down

    def test_stop_before_start_is_safe(self, booted_project):
        stop_exporter()  # must not raise

    def test_second_start_is_a_no_op(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", "http://localhost:4318/v1/traces")
        start_exporter(role="first")
        try:
            tracer_1 = get_tracer()
            start_exporter(role="second")
            assert get_tracer() is tracer_1
        finally:
            stop_exporter()


class TestSpanIsANoOpWhenTracingIsOff:
    def test_span_yields_none_and_never_raises(self, booted_project):
        with span("test.thing", foo="bar") as s:
            assert s is None

    def test_span_body_still_runs(self, booted_project):
        ran = []
        with span("test.thing"):
            ran.append(True)
        assert ran == [True]

    def test_exception_inside_still_propagates(self, booted_project):
        with pytest.raises(ValueError):
            with span("test.thing"):
                raise ValueError("boom")


class TestSpanWithTracingOn:
    def test_span_yields_a_real_span_object(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", "http://localhost:4318/v1/traces")
        start_exporter(role="test-role")
        try:
            with span("test.thing") as s:
                assert s is not None
        finally:
            stop_exporter()

    def test_name_gets_arc_prefix_when_missing(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", "http://localhost:4318/v1/traces")
        start_exporter(role="test-role")
        try:
            with span("test.thing") as s:
                assert s.name == "arc.test.thing"
            with span("arc.already.prefixed") as s2:
                assert s2.name == "arc.already.prefixed"
        finally:
            stop_exporter()


class TestCrossProcessLabels:
    def test_no_active_tracer_returns_empty_labels(self, booted_project):
        assert current_trace_labels() == {}

    def test_active_span_produces_real_labels(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", "http://localhost:4318/v1/traces")
        start_exporter(role="test-role")
        try:
            with span("test.thing"):
                labels = current_trace_labels()
                assert len(labels["arc_trace_id"]) == 32
                assert len(labels["arc_trace_parent_span_id"]) == 16
        finally:
            stop_exporter()

    def test_continue_trace_with_no_tracer_is_a_clean_no_op(self, booted_project):
        ran = []
        with continue_trace({"arc_trace_id": "a" * 32, "arc_trace_parent_span_id": "b" * 16}):
            ran.append(True)
        assert ran == [True]

    def test_continue_trace_with_empty_labels_is_a_clean_no_op(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", "http://localhost:4318/v1/traces")
        start_exporter(role="test-role")
        try:
            ran = []
            with continue_trace({}):
                ran.append(True)
            assert ran == [True]
        finally:
            stop_exporter()

    def test_continue_trace_with_malformed_labels_never_raises(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", "http://localhost:4318/v1/traces")
        start_exporter(role="test-role")
        try:
            ran = []
            with continue_trace({"arc_trace_id": "not-hex!!", "arc_trace_parent_span_id": "also-bad"}):
                ran.append(True)
            assert ran == [True]
        finally:
            stop_exporter()

    def test_continue_trace_round_trips_the_same_trace_id(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", "http://localhost:4318/v1/traces")
        start_exporter(role="test-role")
        try:
            with span("test.enqueuer"):
                labels = current_trace_labels()
            with continue_trace(labels):
                with span("test.job") as job_span:
                    ctx = job_span.get_span_context()
                    assert format(ctx.trace_id, "032x") == labels["arc_trace_id"]
        finally:
            stop_exporter()


# --------------------------------------------------------------------------- #
# Stopping the exporter must not outlast the process supervisor's patience
# --------------------------------------------------------------------------- #
# Found by a load test of a live site whose tracing_otlp_endpoint pointed at a collector that was
# not running: under load the batch processor had spans queued, every export retried against the
# dead endpoint, and OTel's shutdown() waited (its own default is 30 s) - exactly systemd's
# TimeoutStopSec - so `arc restart` hung for 30 s and the workers were killed.


def _dead_endpoint() -> str:
    """http://127.0.0.1:<port>/v1/traces on a port nothing listens on (connections are refused)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1/traces"


def _emit_spans(count: int) -> None:
    for i in range(count):
        with span(f"test.span{i % 10}"):
            pass


class _Collector:
    """A minimal OTLP/HTTP collector: accepts POST /v1/traces and counts the bytes it was sent."""

    def __init__(self) -> None:
        self.bytes_received = 0
        collector = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server's name
                length = int(self.headers.get("Content-Length", 0))
                collector.bytes_received += len(self.rfile.read(length))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # silence
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}/v1/traces"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class TestTimeoutSettings:
    def test_defaults(self, booted_project):
        assert arc.settings.get("tracing_export_timeout_seconds") == 3
        assert arc.settings.get("tracing_shutdown_timeout_seconds") == 5

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("tracing_export_timeout_seconds", "0"),
            ("tracing_export_timeout_seconds", "61"),
            ("tracing_shutdown_timeout_seconds", "-1"),
            ("tracing_shutdown_timeout_seconds", "31"),
        ],
    )
    def test_out_of_range_fails_boot(self, project: Path, key: str, value: str):
        write_lock(project, [])
        arc.boot(project_root=project, entry_points=())
        arc.settings.set(key, value)
        arc.shutdown()
        with pytest.raises(BootError, match=key):
            arc.boot(project_root=project, entry_points=())

    @pytest.mark.parametrize(("key", "value"), [("tracing_export_timeout_seconds", "60"), ("tracing_shutdown_timeout_seconds", "0")])
    def test_boundary_values_boot(self, project: Path, key: str, value: str):
        write_lock(project, [])
        arc.boot(project_root=project, entry_points=())
        arc.settings.set(key, value)
        arc.shutdown()
        arc.boot(project_root=project, entry_points=())


class TestStoppingWithACollectorThatIsDown:
    def test_stop_is_bounded_by_the_shutdown_deadline(self, booted_project, caplog):
        arc.settings.set("tracing_otlp_endpoint", _dead_endpoint())
        arc.settings.set("tracing_export_timeout_seconds", "10")  # each queued batch could take ~10 s on its own
        arc.settings.set("tracing_shutdown_timeout_seconds", "1")
        start_exporter(role="test-role")
        exporter = tracing._exporter
        _emit_spans(3000)
        with caplog.at_level(logging.WARNING, logger="arc.tracing"):
            started = time.monotonic()
            stop_exporter()
            elapsed = time.monotonic() - started
        assert elapsed < 2.5, f"stop_exporter() took {elapsed:.1f}s with a 1 s deadline"
        assert get_tracer() is None
        assert any("did not take the last spans" in r.getMessage() for r in caplog.records)
        assert exporter._shutdown, "the exporter was left retrying after stop_exporter() gave up"

    def test_a_zero_deadline_drops_the_queue_at_once(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", _dead_endpoint())
        arc.settings.set("tracing_shutdown_timeout_seconds", "0")
        start_exporter(role="test-role")
        _emit_spans(3000)
        started = time.monotonic()
        stop_exporter()
        assert time.monotonic() - started < 1.0

    def test_the_default_settings_stop_well_inside_systemds_30_seconds(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", _dead_endpoint())
        start_exporter(role="test-role")
        _emit_spans(3000)
        started = time.monotonic()
        stop_exporter()
        assert time.monotonic() - started < 9.0  # 5 s deadline plus slack; was ~20-40 s

    def test_an_exporter_that_raises_on_shutdown_is_logged_not_raised(self, booted_project, caplog, monkeypatch):
        arc.settings.set("tracing_otlp_endpoint", _dead_endpoint())
        start_exporter(role="test-role")
        provider = tracing._tracer_provider
        monkeypatch.setattr(provider, "shutdown", lambda: (_ for _ in ()).throw(RuntimeError("collector exploded")))
        with caplog.at_level(logging.WARNING, logger="arc.tracing"):
            stop_exporter()  # must not raise
        assert any("collector exploded" in r.getMessage() for r in caplog.records)


class TestExportTimeout:
    def test_the_exporter_gets_the_configured_timeout(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", _dead_endpoint())
        arc.settings.set("tracing_export_timeout_seconds", "7")
        start_exporter(role="test-role")
        try:
            assert tracing._exporter._timeout == 7.0  # OTLPSpanExporter's own attribute
        finally:
            stop_exporter()


class TestStoppingWithAHealthyCollector:
    def test_queued_spans_are_still_delivered_on_stop(self, booted_project):
        collector = _Collector()
        try:
            arc.settings.set("tracing_otlp_endpoint", collector.endpoint)
            start_exporter(role="test-role")
            _emit_spans(50)
            stop_exporter()
            assert collector.bytes_received > 0, "the final flush did not reach a collector that was up"
        finally:
            collector.close()

    def test_a_healthy_stop_does_not_log_a_warning(self, booted_project, caplog):
        collector = _Collector()
        try:
            arc.settings.set("tracing_otlp_endpoint", collector.endpoint)
            start_exporter(role="test-role")
            _emit_spans(50)
            with caplog.at_level(logging.WARNING, logger="arc.tracing"):
                stop_exporter()
            assert not [r for r in caplog.records if r.name == "arc.tracing"]
        finally:
            collector.close()


class TestExporterFailureLogsAreRateLimited:
    """A collector that stays down used to put a line in the journal for every failed batch: 956 in
    30 minutes from two workers. One line per kind per interval, saying how many it held back."""

    def _record(self, msg: str, level: int = logging.ERROR) -> logging.LogRecord:
        return logging.LogRecord("opentelemetry.exporter", level, __file__, 1, msg, (), None)

    def test_the_first_record_passes_and_repeats_inside_the_interval_do_not(self):
        now = [100.0]
        limiter = tracing.RateLimitedLog(60.0, clock=lambda: now[0])
        assert limiter.filter(self._record("Failed to export span batch")) is True
        for _ in range(20):
            now[0] += 1
            assert limiter.filter(self._record("Failed to export span batch")) is False

    def test_after_the_interval_one_passes_and_says_how_many_were_held_back(self):
        now = [100.0]
        limiter = tracing.RateLimitedLog(60.0, clock=lambda: now[0])
        limiter.filter(self._record("Failed to export span batch"))
        for _ in range(7):
            now[0] += 1
            limiter.filter(self._record("Failed to export span batch"))
        now[0] += 60
        record = self._record("Failed to export span batch")
        assert limiter.filter(record) is True
        assert "7 more" in record.getMessage()

    def test_different_messages_and_levels_are_limited_separately(self):
        now = [0.0]
        limiter = tracing.RateLimitedLog(60.0, clock=lambda: now[0])
        assert limiter.filter(self._record("Failed to export span batch")) is True
        assert limiter.filter(self._record("Transient error x", logging.WARNING)) is True
        assert limiter.filter(self._record("Failed to export span batch")) is False

    def test_the_filter_is_installed_once_on_the_exporters_logger(self, booted_project):
        arc.settings.set("tracing_otlp_endpoint", _dead_endpoint())
        exporter_logger = logging.getLogger("opentelemetry.exporter.otlp.proto.http.trace_exporter")
        for _ in range(3):
            start_exporter(role="test-role")
            stop_exporter()
        assert sum(isinstance(f, tracing.RateLimitedLog) for f in exporter_logger.filters) == 1
