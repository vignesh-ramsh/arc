"""arc.tracing — settings declare()/validate_sample_rate() (boot-time range
check), get_tracer()/start_exporter()/stop_exporter() lifecycle, span()'s
clean no-op when tracing is off, and current_trace_labels()/continue_trace()
round-tripping for background-job span continuation. No real OTLP collector
needed for any of this — construction succeeds without a reachable
endpoint (BatchSpanProcessor buffers/exports asynchronously)."""

from __future__ import annotations

from pathlib import Path

import pytest

import arc
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
