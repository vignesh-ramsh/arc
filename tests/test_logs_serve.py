"""`arc logs serve` — the log collector as a process of its own.

This is what the split deployment's `<name>-logs` systemd service runs. The gateway,
the lineup workers and the scheduler find it through ARC_LOG_SOCKET; if it dies they
log to files of their own and reconnect, so restarting it never takes anything else
with it. Tested as the real command in a real subprocess.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from arc.log_collector import LogCollector, default_socket_path

from .test_log_collector import raw_client, read_lines, wait_until

CODE = "from arc.cli import app; app()"


def start_serve(project: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", CODE, "logs", "serve"],
        cwd=project,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def listening(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


@pytest.fixture
def served(project):
    proc = start_serve(project)
    sock = default_socket_path(project)
    assert wait_until(lambda: listening(sock), timeout=20), proc.stderr.read() if proc.poll() else "never listened"
    yield project, sock, proc
    if proc.poll() is None:
        proc.send_signal(signal.SIGKILL)
    proc.wait()


class TestLogsServe:
    def test_it_listens_writes_what_it_receives_and_drains_on_sigterm(self, served):
        project, sock, proc = served
        client = raw_client(sock)
        client.sendall(b'{"n": 1}\n{"n": 2}\n{"n": 3}\n')
        client.close()
        proc.send_signal(signal.SIGTERM)  # right away: nothing but the final drain can have written it yet
        assert proc.wait(timeout=15) == 0
        assert [r["n"] for r in read_lines(project / "logs", "gateway")] == [1, 2, 3]
        assert not sock.exists()  # cleaned up
        assert "stopped" in proc.stdout.read()

    def test_sigint_stops_it_cleanly_too(self, served):
        _, _, proc = served
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=15) == 0

    def test_a_second_instance_refuses_to_start_and_leaves_the_first_working(self, served):
        project, sock, first = served
        second = start_serve(project)
        assert second.wait(timeout=30) == 1
        assert "already running" in second.stderr.read()
        # the running one still has its socket, and still writes
        client = raw_client(sock)
        client.sendall(b'{"still": "working"}\n')
        client.close()
        first.send_signal(signal.SIGTERM)
        assert first.wait(timeout=15) == 0
        assert read_lines(project / "logs", "gateway") == [{"still": "working"}]

    def test_a_stale_socket_file_from_a_crashed_run_does_not_block_it(self, project):
        sock = default_socket_path(project)
        sock.parent.mkdir(parents=True, exist_ok=True)
        leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        leftover.bind(str(sock))
        leftover.close()  # the file stays, nothing listens
        proc = start_serve(project)
        try:
            assert wait_until(lambda: listening(sock), timeout=20), proc.stderr.read() if proc.poll() else "never listened"
        finally:
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=15) == 0

    def test_a_stop_signal_the_instant_the_socket_is_listening_still_drains_and_exits_cleanly(self, project):
        """The signal handlers used to be installed AFTER the socket started listening: a SIGTERM in
        that gap killed the process outright (exit -15) with no final drain. Deterministic here: the
        signal is sent from inside start(), right after the socket is up."""
        code = (
            "import os, signal\n"
            "from arc import log_collector\n"
            "real_start = log_collector.LogCollector.start\n"
            "def start(self):\n"
            "    real_start(self)\n"
            "    os.kill(os.getpid(), signal.SIGTERM)\n"
            "log_collector.LogCollector.start = start\n"
            "from arc.cli import app\n"
            "app()\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, "logs", "serve"], cwd=project, capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        assert "stopped" in proc.stdout
        assert not default_socket_path(project).exists()  # cleaned up, like any clean stop

    def test_a_process_that_logs_through_it_survives_it_being_killed(self, served, tmp_path):
        """The point of a separate log service: kill -9 it and a client keeps logging to
        a file of its own, then goes back once a new one is up."""
        from arc.log import _CategoryRouter, _CollectorHandler
        import logging

        project, sock, proc = served
        fallback = tmp_path / "fallback"
        fallback.mkdir()
        handler = _CollectorHandler(
            str(sock),
            fallback_factory=lambda: _CategoryRouter(fallback, max_bytes=10**9, backup_count=1),
            send_interval=0.005,
            reconnect_backoff=0.2,
        )
        rec = lambda i: logging.LogRecord("gateway.access", logging.INFO, __file__, 1, f"m{i}", (), None)  # noqa: E731
        for i in range(20):
            handler.handle(rec(i))
        handler.flush()
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        for i in range(20, 60):
            handler.handle(rec(i))
        handler.flush(10)
        handler.close()
        assert len(read_lines(fallback, "gateway")) == 40  # everything after the kill landed in its own file
