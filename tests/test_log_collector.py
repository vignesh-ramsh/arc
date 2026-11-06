"""The log collector — the single writer behind logs/*.jsonl under `arc run` — and
the per-process handler that feeds it (arc/log_collector.py, arc/log.py).

What this pins, in the order a request meets it:
  * a log call never waits on the collector or the disk (queue full => drop and
    count; collector stalled or gone => the sender thread writes the files directly);
  * the collector writes whole lines only, in chunks, and is the only thing that
    rotates (no line is lost to rotation — 8 processes, forced rotations, 100%);
  * every failure path degrades to "write the files directly" rather than to silence.
"""

from __future__ import annotations

import concurrent.futures
import glob
import json
import logging
import multiprocessing
import os
import socket
import stat
import tempfile
import time
import types
from pathlib import Path

import pytest

from arc import log
from arc.log import _CategoryRouter, _CollectorHandler
from arc.log_collector import LogCollector, default_socket_path


# --------------------------------------------------------------------------- helpers
def wait_until(predicate, timeout: float = 5.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def read_lines(logs_dir: Path, category: str) -> list[dict]:
    """Every line of a category, oldest first (rotated files carry higher
    numbers the older they are). A torn line fails json.loads — which is the point."""
    rotated = sorted(
        glob.glob(str(logs_dir / f"{category}.jsonl.*")),
        key=lambda p: int(p.rsplit(".", 1)[1]),
        reverse=True,
    )
    out: list[dict] = []
    for path in [*rotated, str(logs_dir / f"{category}.jsonl")]:
        if os.path.exists(path):
            with open(path) as handle:
                out.extend(json.loads(line) for line in handle)
    return out


def raw_client(path: Path, category: bytes = b"gateway") -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    client.sendall(b"CAT " + category + b"\n")
    return client


def record(name: str = "gateway.access", message: str = "GET /x -> 200 (1ms)", **extra) -> logging.LogRecord:
    rec = logging.LogRecord(name, logging.INFO, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


@pytest.fixture
def collector_factory(tmp_path):
    made: list[LogCollector] = []

    def make(**kwargs) -> LogCollector:
        kwargs.setdefault("max_bytes", 10**9)
        kwargs.setdefault("backup_count", 5)
        kwargs.setdefault("flush_seconds", 0.005)
        collector = LogCollector(tmp_path / "logs", tmp_path / "s.sock", **kwargs)
        collector.start()
        made.append(collector)
        return collector

    yield make
    for collector in made:
        collector.stop()


def make_handler(collector: LogCollector, tmp_path: Path, **kwargs) -> _CollectorHandler:
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir(exist_ok=True)
    kwargs.setdefault("send_interval", 0.005)
    return _CollectorHandler(
        str(collector.socket_path),
        fallback_factory=lambda: _CategoryRouter(fallback_dir, max_bytes=10**9, backup_count=1),
        **kwargs,
    )


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    router, sink = log._router, log._sink
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    log._router, log._sink = router, sink


# --------------------------------------------------------------------------- the collector
class TestCollectorWrites:
    def test_lines_sent_by_a_client_land_in_the_category_file(self, collector_factory):
        collector = collector_factory()
        with raw_client(collector.socket_path) as client:
            client.sendall(b'{"a": 1}\n{"a": 2}\n')
        collector.stop()
        assert read_lines(collector.logs_dir, "gateway") == [{"a": 1}, {"a": 2}]

    def test_each_category_goes_to_its_own_file(self, collector_factory):
        collector = collector_factory()
        with raw_client(collector.socket_path, b"gateway") as g, raw_client(collector.socket_path, b"system") as s:
            g.sendall(b'{"from": "gateway"}\n')
            s.sendall(b'{"from": "system"}\n')
        collector.stop()
        assert read_lines(collector.logs_dir, "gateway") == [{"from": "gateway"}]
        assert read_lines(collector.logs_dir, "system") == [{"from": "system"}]

    def test_a_line_split_across_sends_is_reassembled_whole(self, collector_factory):
        collector = collector_factory()
        with raw_client(collector.socket_path) as client:
            client.sendall(b'{"long": "he')
            time.sleep(0.05)
            client.sendall(b'llo"}\n')
        collector.stop()
        assert read_lines(collector.logs_dir, "gateway") == [{"long": "hello"}]

    def test_many_senders_are_written_in_a_few_chunks_not_line_by_line(self, collector_factory):
        collector = collector_factory(flush_seconds=0.2)
        clients = [raw_client(collector.socket_path) for _ in range(20)]
        for index, client in enumerate(clients):
            client.sendall(b"".join(b'{"c": %d, "i": %d}\n' % (index, i) for i in range(100)))
        for client in clients:
            client.close()
        collector.stop()
        assert collector.lines_written == 2000
        assert collector.writes <= 5, f"{collector.writes} write() calls for 2000 lines"

    def test_stop_writes_what_senders_already_put_on_the_wire(self, collector_factory):
        """flush window far longer than the test: only the final drain can write these."""
        collector = collector_factory(flush_seconds=30)
        client = raw_client(collector.socket_path)
        client.sendall(b'{"n": 1}\n{"n": 2}\n{"n": 3}\n')
        collector.stop()
        client.close()
        assert [row["n"] for row in read_lines(collector.logs_dir, "gateway")] == [1, 2, 3]

    def test_a_partial_trailing_line_from_a_killed_sender_is_never_written(self, collector_factory):
        collector = collector_factory()
        with raw_client(collector.socket_path) as client:
            client.sendall(b'{"whole": true}\n{"par')
        assert wait_until(lambda: collector.discarded_partial_bytes == len(b'{"par'))
        collector.stop()
        assert read_lines(collector.logs_dir, "gateway") == [{"whole": True}]


class TestCollectorRotation:
    def test_no_line_is_lost_across_many_rotations(self, collector_factory):
        collector = collector_factory(max_bytes=20_000, backup_count=1000)
        with raw_client(collector.socket_path) as client:
            for chunk in range(40):
                client.sendall(b"".join(b'{"seq": %d, "pad": "%s"}\n' % (chunk * 500 + i, b"x" * 60) for i in range(500)))
        collector.stop()
        assert collector.rotations >= 3
        assert [row["seq"] for row in read_lines(collector.logs_dir, "gateway")] == list(range(20_000))

    def test_only_backup_count_old_files_are_kept(self, collector_factory):
        collector = collector_factory(max_bytes=5_000, backup_count=2)
        with raw_client(collector.socket_path) as client:
            for chunk in range(40):
                client.sendall(b"".join(b'{"seq": %d, "pad": "%s"}\n' % (chunk * 50 + i, b"x" * 60) for i in range(50)))
                time.sleep(0.01)
        collector.stop()
        names = sorted(p.name for p in collector.logs_dir.glob("gateway.jsonl*"))
        assert collector.rotations > 2
        assert set(names) <= {"gateway.jsonl", "gateway.jsonl.1", "gateway.jsonl.2"}

    def test_a_file_renamed_from_outside_is_followed_not_written_through(self, collector_factory):
        """A CLI command's own handler (or logrotate, or a person) can rotate a file
        under the collector. It must write to the path, not to the moved inode."""
        collector = collector_factory()
        logs = collector.logs_dir
        client = raw_client(collector.socket_path)
        client.sendall(b'{"n": "before"}\n')
        assert wait_until(lambda: (logs / "gateway.jsonl").exists() and (logs / "gateway.jsonl").stat().st_size > 0)
        os.rename(logs / "gateway.jsonl", logs / "gateway.jsonl.moved")
        client.sendall(b'{"n": "after"}\n')
        assert wait_until(lambda: (logs / "gateway.jsonl").exists())
        client.close()
        collector.stop()
        assert json.loads((logs / "gateway.jsonl.moved").read_text()) == {"n": "before"}
        assert json.loads((logs / "gateway.jsonl").read_text()) == {"n": "after"}


class TestCollectorRobustness:
    def test_a_category_that_could_escape_the_logs_dir_is_rejected(self, collector_factory, tmp_path):
        collector = collector_factory()
        with raw_client(collector.socket_path, b"../evil") as client:
            client.sendall(b'{"x": 1}\n')
            assert wait_until(lambda: collector.rejected == 1)
        collector.stop()
        assert not (tmp_path / "evil.jsonl").exists()
        assert not list(collector.logs_dir.glob("*evil*"))

    def test_garbage_that_never_ends_a_handshake_is_rejected(self, collector_factory):
        collector = collector_factory()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(collector.socket_path))
        client.sendall(b"x" * 500)
        assert wait_until(lambda: collector.rejected == 1)
        client.close()

    def test_the_socket_is_private_and_removed_on_stop(self, collector_factory):
        collector = collector_factory()
        assert stat.S_IMODE(os.stat(collector.socket_path).st_mode) == 0o600
        collector.stop()
        assert not collector.socket_path.exists()

    def test_a_stale_socket_file_from_a_crashed_run_is_replaced(self, tmp_path):
        stale = tmp_path / "s.sock"
        leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        leftover.bind(str(stale))
        leftover.close()  # file stays behind, nothing listening
        collector = LogCollector(tmp_path / "logs", stale, max_bytes=10**9, backup_count=1)
        collector.start()
        try:
            with raw_client(collector.socket_path) as client:
                client.sendall(b'{"ok": 1}\n')
        finally:
            collector.stop()
        assert read_lines(tmp_path / "logs", "gateway") == [{"ok": 1}]

    def test_stopping_twice_is_harmless(self, collector_factory):
        collector = collector_factory()
        collector.stop()
        collector.stop()


class TestSocketPath:
    def test_a_normal_project_uses_its_runtime_dir(self, tmp_path):
        assert default_socket_path(tmp_path) == tmp_path / ".arc" / "runtime" / "log.sock"

    def test_a_path_too_long_for_a_unix_socket_falls_back_to_a_short_private_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        deep = tmp_path / ("d" * 120)
        chosen = default_socket_path(deep)
        assert len(os.fsencode(chosen)) <= 100
        assert chosen.parent.name.startswith("arc-")
        collector = LogCollector(tmp_path / "logs", chosen, max_bytes=10**9, backup_count=1)
        collector.start()  # and it really binds
        try:
            assert stat.S_IMODE(os.stat(chosen.parent).st_mode) == 0o700
            with raw_client(chosen) as client:
                client.sendall(b'{"deep": true}\n')
        finally:
            collector.stop()
        assert read_lines(tmp_path / "logs", "gateway") == [{"deep": True}]


# --------------------------------------------------------------------------- the handler
class TestHandlerDelivers:
    def test_records_arrive_as_json_lines_in_their_category(self, collector_factory, tmp_path):
        collector = collector_factory()
        handler = make_handler(collector, tmp_path)
        handler.handle(record("gateway.access", "GET /a -> 200 (1ms)", path="/a", status=200))
        handler.handle(record("authn", "login ok"))
        handler.handle(record("relay.crud", "saved"))
        handler.close()
        collector.stop()
        (gateway,) = read_lines(collector.logs_dir, "gateway")
        assert gateway["logger"] == "gateway.access" and gateway["path"] == "/a" and gateway["status"] == 200
        assert read_lines(collector.logs_dir, "system")[0]["message"] == "login ok"
        assert read_lines(collector.logs_dir, "relay")[0]["message"] == "saved"
        assert handler.sent == 3 and handler.fallback_lines == 0 and handler.dropped == 0

    def test_message_arguments_are_resolved_when_the_call_is_made(self, collector_factory, tmp_path):
        collector = collector_factory()
        handler = make_handler(collector, tmp_path)
        rec = logging.LogRecord("gateway", logging.INFO, __file__, 1, "user %s did %d", ("ann", 3), None)
        handler.handle(rec)
        handler.close()
        collector.stop()
        assert read_lines(collector.logs_dir, "gateway")[0]["message"] == "user ann did 3"

    def test_emit_is_cheap_even_with_a_live_collector(self, collector_factory, tmp_path):
        collector = collector_factory()
        handler = make_handler(collector, tmp_path)
        rec = record()
        n = 20_000
        start = time.perf_counter()
        for _ in range(n):
            handler.handle(rec)
        per_call_us = (time.perf_counter() - start) / n * 1e6
        handler.close()
        collector.stop()
        assert per_call_us < 100, f"{per_call_us:.1f} us per log call"  # the old path: ~18 us + a write()


class _StalledServer:
    """Accepts connections and never reads: a collector that is alive but stuck."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(8)
        self.accepted: list[socket.socket] = []

    def accept_some(self, n: int = 1) -> None:
        for _ in range(n):
            self.srv.settimeout(2)
            self.accepted.append(self.srv.accept()[0])

    def close(self) -> None:
        for conn in self.accepted:
            conn.close()
        self.srv.close()


class TestHandlerNeverBlocksTheCaller:
    def test_a_stalled_collector_costs_the_caller_nothing_and_loses_nothing(self, tmp_path):
        stalled = _StalledServer(tmp_path / "stalled.sock")
        fallback_dir = tmp_path / "fallback"
        fallback_dir.mkdir()
        handler = _CollectorHandler(
            str(stalled.path),
            fallback_factory=lambda: _CategoryRouter(fallback_dir, max_bytes=10**9, backup_count=1),
            send_interval=0.005,
            send_timeout=0.3,
            queue_size=50_000,
        )
        try:
            n = 30_000
            rec = record(path="/api/v1/benchplug.get_item", client_ip="127.0.0.1", identity="bench@example.com")
            start = time.perf_counter()
            for _ in range(n):
                handler.handle(rec)
            emit_seconds = time.perf_counter() - start
            handler.flush(30)
            assert emit_seconds < 3.0, f"30,000 log calls took {emit_seconds:.1f}s while the collector was stuck"
            handler.close()
            assert handler.sent + handler.fallback_lines + handler.dropped == n
            assert handler.sent == 0 or handler.sent < n  # nothing "sent" that a stuck reader never took
            assert len(read_lines(fallback_dir, "gateway")) == handler.fallback_lines
            assert handler.fallback_lines > 0
        finally:
            stalled.close()

    def test_a_full_queue_drops_and_counts_instead_of_blocking(self, tmp_path):
        stalled = _StalledServer(tmp_path / "stalled.sock")
        fallback_dir = tmp_path / "fallback"
        fallback_dir.mkdir()
        handler = _CollectorHandler(
            str(stalled.path),
            fallback_factory=lambda: _CategoryRouter(fallback_dir, max_bytes=10**9, backup_count=1),
            send_interval=0.05,  # the sender is slow to drain...
            send_timeout=0.2,
            queue_size=100,  # ...and the queue is tiny
        )
        try:
            start = time.perf_counter()
            for _ in range(5_000):
                handler.handle(record())
            assert time.perf_counter() - start < 2.0
            assert handler.dropped > 0
            handler.close()
            assert handler.sent + handler.fallback_lines + handler.dropped == 5_000
        finally:
            stalled.close()


class TestHandlerFallsBackAndRecovers:
    def test_with_no_collector_at_all_the_records_still_reach_the_files(self, tmp_path):
        fallback_dir = tmp_path / "fallback"
        fallback_dir.mkdir()
        handler = _CollectorHandler(
            str(tmp_path / "nobody-listens.sock"),
            fallback_factory=lambda: _CategoryRouter(fallback_dir, max_bytes=10**9, backup_count=1),
            send_interval=0.005,
        )
        for i in range(200):
            handler.handle(record(seq=i))
        handler.close()
        assert [row["seq"] for row in read_lines(fallback_dir, "gateway")] == list(range(200))
        assert handler.fallback_lines == 200 and handler.sent == 0

    def test_if_the_collector_dies_mid_run_nothing_is_lost_and_nothing_hangs(self, collector_factory, tmp_path):
        collector = collector_factory()
        handler = make_handler(collector, tmp_path, reconnect_backoff=60)
        for i in range(50):
            handler.handle(record(seq=i))
        handler.flush()
        assert wait_until(lambda: collector.lines_written == 50)
        collector.stop()  # the collector goes away

        start = time.perf_counter()
        for i in range(50, 550):
            handler.handle(record(seq=i))
        assert time.perf_counter() - start < 1.0
        handler.close()
        assert [r["seq"] for r in read_lines(collector.logs_dir, "gateway")] == list(range(50))
        assert [r["seq"] for r in read_lines(tmp_path / "fallback", "gateway")] == list(range(50, 550))

    def test_it_goes_back_to_the_collector_when_one_returns(self, tmp_path):
        logs = tmp_path / "logs"
        first = LogCollector(logs, tmp_path / "s.sock", max_bytes=10**9, backup_count=1, flush_seconds=0.005)
        first.start()
        handler = make_handler(first, tmp_path, reconnect_backoff=0.1)
        handler.handle(record(seq=0))
        handler.flush()
        first.stop()
        for i in range(1, 6):  # collector gone: these go to the fallback
            handler.handle(record(seq=i))
        handler.flush()
        second = LogCollector(logs, tmp_path / "s.sock", max_bytes=10**9, backup_count=1, flush_seconds=0.005)
        second.start()
        try:
            time.sleep(0.3)  # past the backoff
            for i in range(6, 11):
                handler.handle(record(seq=i))
            handler.flush()
            assert wait_until(lambda: second.lines_written >= 5)
            handler.close()
        finally:
            second.stop()
        assert [r["seq"] for r in read_lines(logs, "gateway")] == [0, *range(6, 11)]
        assert [r["seq"] for r in read_lines(tmp_path / "fallback", "gateway")] == list(range(1, 6))


class TestFallbackNeverTouchesTheSharedFiles:
    """Measured on a real `arc run` with the collector killed mid-load: when the
    fallback shared the collector's files, 8 processes rotating them independently
    pushed already-written lines out of the backup window and only 48% survived."""

    def test_the_fallback_router_writes_files_of_its_own(self, tmp_path, monkeypatch, restore_root_logging):
        monkeypatch.delenv(log.COLLECTOR_ENV, raising=False)
        log.configure(types.SimpleNamespace(settings=None, project_root=tmp_path))
        router = log._fallback_router()
        router.emit(record("gateway.access"))
        router.close()
        names = sorted(p.name for p in (tmp_path / "logs").glob("gateway*.jsonl*"))
        assert names == [f"gateway.fallback-{os.getpid()}.jsonl"]

    def test_after_the_collector_dies_lines_go_to_per_process_files_and_the_shared_file_is_untouched(
        self, collector_factory, tmp_path, monkeypatch, restore_root_logging
    ):
        collector = collector_factory()
        monkeypatch.setenv(log.COLLECTOR_ENV, str(collector.socket_path))
        log.configure(types.SimpleNamespace(settings=None, project_root=tmp_path))
        logs = tmp_path / "logs"
        assert isinstance(log._router, _CollectorHandler)
        for i in range(100):
            logging.getLogger("gateway.access").warning("before", extra={"seq": i})
        log._router.flush()
        assert wait_until(lambda: collector.lines_written == 100)
        shared_before = (logs / "gateway.jsonl").read_bytes()

        collector.stop()  # the collector disappears
        for i in range(100, 400):
            logging.getLogger("gateway.access").warning("after", extra={"seq": i})
        log._router.flush(10)

        assert (logs / "gateway.jsonl").read_bytes() == shared_before  # not appended to, not rotated
        assert not list(logs.glob("gateway.jsonl.*"))
        fallback = [json.loads(line) for line in (logs / f"gateway.fallback-{os.getpid()}.jsonl").read_text().splitlines()]
        assert [row["seq"] for row in fallback] == list(range(100, 400))


class TestHandlerAfterFork:
    def test_a_forked_child_logs_through_its_own_connection(self, collector_factory, tmp_path):
        """Sharing the parent's connections would interleave two processes' bytes
        mid-line, and the parent's sender thread does not exist in the child."""
        collector = collector_factory()
        handler = make_handler(collector, tmp_path)
        handler.handle(record(seq=-1))
        handler.flush()

        def child() -> None:
            for i in range(100):
                handler.handle(record(seq=i, who="child"))
            handler.flush()
            os._exit(0)

        process = multiprocessing.get_context("fork").Process(target=child)
        process.start()
        process.join(30)
        assert process.exitcode == 0
        handler.close()
        collector.stop()
        rows = read_lines(collector.logs_dir, "gateway")
        assert sum(1 for r in rows if r.get("who") == "child") == 100
        assert any(r["seq"] == -1 for r in rows)


# --------------------------------------------------------------------------- wiring
class TestConfigure:
    def kernel(self, tmp_path):
        return types.SimpleNamespace(settings=None, project_root=tmp_path)

    def test_with_a_live_collector_in_the_environment_the_collector_handler_is_used(
        self, collector_factory, tmp_path, monkeypatch, restore_root_logging
    ):
        collector = collector_factory()
        monkeypatch.setenv(log.COLLECTOR_ENV, str(collector.socket_path))
        log.configure(self.kernel(tmp_path))
        assert isinstance(log._router, _CollectorHandler)
        assert log.stats()["mode"] == "collector"

    def test_a_stale_environment_variable_falls_back_to_writing_directly(
        self, tmp_path, monkeypatch, restore_root_logging
    ):
        monkeypatch.setenv(log.COLLECTOR_ENV, str(tmp_path / "gone.sock"))
        log.configure(self.kernel(tmp_path))
        assert isinstance(log._router, _CategoryRouter)
        assert log.stats() == {"mode": "direct"}

    def test_without_the_variable_it_writes_directly_as_before(self, tmp_path, monkeypatch, restore_root_logging):
        monkeypatch.delenv(log.COLLECTOR_ENV, raising=False)
        log.configure(self.kernel(tmp_path))
        assert isinstance(log._router, _CategoryRouter)

    def test_arc_run_can_switch_itself_onto_the_collector_and_back_without_losing_records(
        self, collector_factory, tmp_path, monkeypatch, restore_root_logging
    ):
        monkeypatch.delenv(log.COLLECTOR_ENV, raising=False)
        log.configure(self.kernel(tmp_path))  # booted before the collector existed
        collector = collector_factory()
        logging.getLogger("system.probe").warning("before switch")
        assert log.use_collector(collector.socket_path) is True
        assert isinstance(log._router, _CollectorHandler)
        logging.getLogger("system.probe").warning("via collector")
        log.use_direct()
        assert isinstance(log._router, _CategoryRouter)
        logging.getLogger("system.probe").warning("after switch back")
        collector.stop()
        for handler in logging.getLogger().handlers:
            handler.flush()
        messages = [r["message"] for r in read_lines(tmp_path / "logs", "system")]
        assert messages == ["before switch", "via collector", "after switch back"]

    def test_use_collector_declines_when_nothing_is_listening(self, tmp_path, monkeypatch, restore_root_logging):
        monkeypatch.delenv(log.COLLECTOR_ENV, raising=False)
        log.configure(self.kernel(tmp_path))
        assert log.use_collector(tmp_path / "nobody.sock") is False
        assert isinstance(log._router, _CategoryRouter)


# --------------------------------------------------------------------------- the acceptance test
def _stress_worker(socket_path: str, fallback_dir: str, worker: int, n: int):
    """One OS process standing in for one gateway worker."""
    from pathlib import Path as _Path

    from arc import log as _log

    handler = _log._CollectorHandler(
        socket_path,
        fallback_factory=lambda: _log._CategoryRouter(_Path(fallback_dir), max_bytes=10**9, backup_count=1),
        send_interval=0.005,
    )
    for i in range(n):
        rec = logging.LogRecord("gateway.access", logging.INFO, "x", 1, "GET /api/v1/x -> 200 (1ms)", (), None)
        rec.w, rec.seq, rec.path, rec.identity = worker, i, "/api/v1/benchplug.get_item", "bench@example.com"
        handler.handle(rec)
    handler.close()
    return handler.dropped, handler.fallback_lines


class TestEightProcessesOneFileEveryLineSurvives:
    def test_forced_rotations_lose_nothing_and_tear_nothing(self, collector_factory, tmp_path):
        workers, n = 8, 5_000
        collector = collector_factory(max_bytes=200_000, backup_count=1000)
        ctx = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            futures = [
                pool.submit(_stress_worker, str(collector.socket_path), str(tmp_path / "fallback"), w, n)
                for w in range(workers)
            ]
            results = [f.result(timeout=120) for f in futures]
        collector.stop()

        assert results == [(0, 0)] * workers  # nothing dropped, nothing fell back
        assert collector.rotations >= 10, collector.rotations
        rows = read_lines(collector.logs_dir, "gateway")  # json.loads on every line: none torn
        assert len(rows) == workers * n
        for w in range(workers):
            assert [r["seq"] for r in rows if r["w"] == w] == list(range(n)), f"worker {w}: lines missing or out of order"
        assert not (tmp_path / "fallback").exists() or not list((tmp_path / "fallback").glob("*.jsonl"))
