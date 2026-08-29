"""arc.metrics — cross-process telemetry: the duck-typed collect() (mirrors
arc.health.check()'s convention exactly), the per-pid file read/prune
(mirrors arc.events.register_process()/list_processes()), aggregate()'s
cross-process summing, format_prometheus()'s exact text output, and the
start_exporter()/stop_exporter() background writer end to end."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import arc
from arc.metrics import MetricsError, aggregate, format_prometheus, read_all, start_exporter, stop_exporter
from arc.metrics import collect as metrics_collect

from .conftest import FakeEntryPoint, write_lock


class TestCollect:
    def test_capability_without_metrics_is_skipped(self, project: Path):
        write_lock(project, [{"name": "widget"}])
        kernel = arc.boot(
            project_root=project,
            entry_points=[FakeEntryPoint("widget", lambda k: k.export("widget", object()))],
        )
        assert metrics_collect(kernel) == {}

    def test_capability_with_metrics_is_collected(self, project: Path):
        class Widget:
            def metrics(self):
                return {"count": 3}

        write_lock(project, [{"name": "widget"}])
        kernel = arc.boot(
            project_root=project,
            entry_points=[FakeEntryPoint("widget", lambda k: k.export("widget", Widget()))],
        )
        assert metrics_collect(kernel) == {"widget": {"count": 3}}

    def test_a_raising_metrics_method_is_isolated_not_fatal(self, project: Path):
        class Good:
            def metrics(self):
                return {"count": 1}

        class Bad:
            def metrics(self):
                raise ValueError("boom")

        write_lock(project, [{"name": "good"}, {"name": "bad"}])
        kernel = arc.boot(
            project_root=project,
            entry_points=[
                FakeEntryPoint("good", lambda k: k.export("good", Good())),
                FakeEntryPoint("bad", lambda k: k.export("bad", Bad())),
            ],
        )
        results = metrics_collect(kernel)
        assert results["good"] == {"count": 1}
        assert "boom" in results["bad"]["error"]


class TestReadAllPruning:
    def _write_pid_file(self, project: Path, pid: int, data: dict) -> Path:
        directory = project / ".arc" / "runtime" / "metrics"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{pid}.json"
        path.write_text(json.dumps({"pid": pid, "role": "test", "written_at": time.time(), "data": data}))
        return path

    def test_no_directory_returns_empty(self, project: Path):
        assert read_all(project) == []

    def test_live_pid_is_kept(self, project: Path):
        import os

        self._write_pid_file(project, os.getpid(), {"widget": {"count": 1}})
        results = read_all(project)
        assert len(results) == 1
        assert results[0]["data"] == {"widget": {"count": 1}}

    def test_dead_pid_is_pruned_and_removed(self, project: Path):
        dead_pid = 999_999_999  # astronomically unlikely to be a real live pid
        path = self._write_pid_file(project, dead_pid, {"widget": {"count": 1}})
        results = read_all(project)
        assert results == []
        assert not path.exists()

    def test_corrupt_file_is_pruned_without_raising(self, project: Path):
        directory = project / ".arc" / "runtime" / "metrics"
        directory.mkdir(parents=True, exist_ok=True)
        bad_path = directory / "garbage.json"
        bad_path.write_text("not valid json{{{")
        assert read_all(project) == []
        assert not bad_path.exists()


class TestAggregate:
    def test_numeric_fields_sum_across_snapshots(self):
        snapshots = [
            {"data": {"gateway": {"requests_total": 10}}},
            {"data": {"gateway": {"requests_total": 25}}},
        ]
        assert aggregate(snapshots) == {"gateway": {"requests_total": 35}}

    def test_nested_dicts_sum_leaf_values(self):
        snapshots = [
            {"data": {"gateway": {"requests_by_status_class": {"2xx": 5, "4xx": 1}}}},
            {"data": {"gateway": {"requests_by_status_class": {"2xx": 3, "5xx": 2}}}},
        ]
        result = aggregate(snapshots)
        assert result["gateway"]["requests_by_status_class"] == {"2xx": 8, "4xx": 1, "5xx": 2}

    def test_bool_fields_are_never_summed(self):
        snapshots = [{"data": {"pgdb": {"ok": True, "size": 5}}}]
        result = aggregate(snapshots)
        assert "ok" not in result["pgdb"]
        assert result["pgdb"]["size"] == 5

    def test_non_numeric_fields_are_skipped(self):
        snapshots = [{"data": {"pgdb": {"version": "16.1", "size": 5}}}]
        result = aggregate(snapshots)
        assert "version" not in result["pgdb"]
        assert result["pgdb"]["size"] == 5

    def test_empty_snapshots_produce_empty_aggregate(self):
        assert aggregate([]) == {}


class TestFormatPrometheus:
    def test_scalar_field_becomes_one_metric_line(self):
        text = format_prometheus({"gateway": {"requests_total": 42}})
        assert "# TYPE arc_gateway_requests_total counter" in text
        assert "arc_gateway_requests_total 42" in text

    def test_sum_suffix_is_typed_as_counter_not_gauge(self):
        text = format_prometheus({"gateway": {"request_duration_ms_sum": 12.5}})
        assert "# TYPE arc_gateway_request_duration_ms_sum counter" in text

    def test_plain_field_is_typed_as_gauge(self):
        text = format_prometheus({"pgdb": {"size": 5}})
        assert "# TYPE arc_pgdb_size gauge" in text

    def test_nested_dict_becomes_one_line_per_bucket(self):
        text = format_prometheus({"gateway": {"requests_by_status_class": {"2xx": 8, "4xx": 1}}})
        assert 'arc_gateway_requests_by_status_class{bucket="2xx"} 8' in text
        assert 'arc_gateway_requests_by_status_class{bucket="4xx"} 1' in text

    def test_output_ends_with_a_trailing_newline(self):
        text = format_prometheus({"gateway": {"requests_total": 1}})
        assert text.endswith("\n")


class TestExporterLifecycle:
    async def test_start_before_boot_raises(self):
        with pytest.raises(MetricsError):
            start_exporter(role="test-role")

    async def test_start_writes_a_pid_file_stop_removes_it(self, project: Path):
        write_lock(project, [])
        arc.boot(project_root=project, entry_points=[FakeEntryPoint("noop", lambda k: None)])
        try:
            start_exporter(role="test-role", interval_seconds=0.05)
            await asyncio.sleep(0.15)  # let the loop tick at least once
            results = read_all(project)
            assert len(results) == 1
            assert results[0]["role"] == "test-role"
        finally:
            await stop_exporter()
        assert read_all(project) == []

    async def test_second_start_is_a_no_op(self, project: Path):
        write_lock(project, [])
        arc.boot(project_root=project, entry_points=[FakeEntryPoint("noop", lambda k: None)])
        try:
            start_exporter(role="first", interval_seconds=5.0)
            start_exporter(role="second", interval_seconds=5.0)  # must not replace the running task
            await asyncio.sleep(0.05)
            results = read_all(project)
            assert len(results) == 1
            assert results[0]["role"] == "first"
        finally:
            await stop_exporter()
