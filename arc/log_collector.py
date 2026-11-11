"""
arc.log_collector
------------------
The single writer behind ``logs/*.jsonl`` while ``arc run`` is up.

Why it exists. Every gateway worker and every lineup process used to own a
``RotatingFileHandler`` on the SAME files. Stdlib rotation is not safe across
processes (each one decides on its own that the file is full and renames it
while the others keep writing to the moved inode), and under sustained load
about a third of the lines vanished (measured: 67% retained at ~8,750 req/s).
It also meant one ``write()`` + ``flush()`` and a couple of size-check syscalls
per log line, on the request path.

How it works. ``arc run`` starts a LogCollector before it spawns anything and
puts its socket path in ``ARC_LOG_SOCKET``. Each process's log handler (see
``arc.log._CollectorHandler``) keeps a small in-process queue — the request path
only does ``put_nowait`` — and a sender thread that, every few milliseconds,
sends whatever has accumulated over a Unix stream socket. This module is the
other end: it reads from every connection, and at most every ``flush_seconds``
(or ``MAX_BATCH_BYTES``) it appends each category's accumulated lines with ONE
``os.write``. Because it is the only process that ever writes or rotates these
files there is no rotation race and no locking.

Wire protocol (one connection per process per category):

    CAT <category>\\n            handshake; category matches [a-z][a-z0-9_]{0,31}
    <json line>\\n ...           any number of complete lines, in any chunking

Only whole lines are ever written. A partial trailing line (a sender killed
mid-send) is discarded and counted, never written torn.

The collector never blocks a sender: if it is slow, kernel socket buffers fill
and the SENDER THREAD waits (and, past its timeout, falls back to writing the
files directly) — the request path is not involved.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import permissions

DEFAULT_FLUSH_SECONDS = 0.010
#: Flush early once this many bytes are pending, whatever the interval says.
MAX_BATCH_BYTES = 256 * 1024
RECV_BYTES = 256 * 1024
#: A connection that sends this much without a newline is not sending log lines.
MAX_LINE_BYTES = 256 * 1024
#: AF_UNIX paths are limited to ~107 bytes; stay safely under.
_UNIX_PATH_LIMIT = 100
_CATEGORY_RE = re.compile(rb"^[a-z][a-z0-9_]{0,31}$")
_HANDSHAKE_MAX = 64


def default_socket_path(project_root: Path) -> Path:
    """``<project>/.arc/runtime/log.sock``, unless that is too long for a Unix
    socket (a deep checkout), in which case a short per-user path under the
    system temp dir derived from the project path."""
    preferred = project_root / ".arc" / "runtime" / "log.sock"
    if len(os.fsencode(preferred)) <= _UNIX_PATH_LIMIT:
        return preferred
    digest = hashlib.sha1(os.fsencode(project_root.resolve())).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"arc-{os.getuid()}" / f"{digest}.sock"


class _Conn:
    __slots__ = ("sock", "category", "buf")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.category: str | None = None
        self.buf = b""


class _OpenLog:
    __slots__ = ("fd", "size", "ino")

    def __init__(self, fd: int, size: int, ino: int) -> None:
        self.fd, self.size, self.ino = fd, size, ino


class LogCollector:
    def __init__(
        self,
        logs_dir: Path,
        socket_path: Path,
        *,
        max_bytes: int,
        backup_count: int,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
    ) -> None:
        self.logs_dir = Path(logs_dir)
        self.socket_path = Path(socket_path)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._flush_seconds = flush_seconds

        # Counters, written only by the collector thread; read freely elsewhere.
        self.lines_written = 0
        self.writes = 0
        self.rotations = 0
        self.connections = 0
        self.rejected = 0
        self.write_errors = 0
        self.discarded_partial_bytes = 0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._srv: socket.socket | None = None
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._files: dict[str, _OpenLog] = {}
        self._pending: dict[str, list[bytes]] = {}
        self._pending_bytes = 0
        self._last_error_report = 0.0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        permissions.create_private_dir(self.logs_dir, permissions.SHARED_DIR_MODE)
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if parent.name.startswith("arc-"):  # the private temp-dir fallback
            os.chmod(parent, 0o700)
        # A stale socket from a crashed run: `arc run` holds the project's run
        # lock, so no live collector can own this path.
        self.socket_path.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            srv.listen(128)
            srv.setblocking(False)
        except OSError:
            srv.close()
            raise
        self._srv = srv
        self._thread = threading.Thread(target=self._run, name="arc-log-collector", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Drain what the senders already put on the wire, write it, close."""
        if self._thread is None:
            return
        self._stop.set()
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass
        self._thread.join(timeout)
        self._thread = None
        for resource in (self._srv, self._wake_r, self._wake_w):
            if resource is not None:
                try:
                    resource.close()
                except OSError:
                    pass
        self._srv = None
        self.socket_path.unlink(missing_ok=True)

    def stats(self) -> dict[str, int]:
        return {
            "lines_written": self.lines_written,
            "writes": self.writes,
            "rotations": self.rotations,
            "connections": self.connections,
            "rejected": self.rejected,
            "write_errors": self.write_errors,
            "discarded_partial_bytes": self.discarded_partial_bytes,
        }

    # ------------------------------------------------------------------ #
    # the loop
    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        assert self._srv is not None
        sel = selectors.DefaultSelector()
        sel.register(self._srv, selectors.EVENT_READ, "accept")
        sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        conns: dict[socket.socket, _Conn] = {}
        last_flush = time.monotonic()
        try:
            while True:
                timeout = None
                if self._pending:
                    timeout = max(0.0, last_flush + self._flush_seconds - time.monotonic())
                for key, _events in sel.select(timeout):
                    tag = key.data
                    if tag == "accept":
                        self._accept(sel, conns)
                    elif tag == "wake":
                        try:
                            self._wake_r.recv(4096)
                        except BlockingIOError:
                            pass
                    else:
                        self._read(sel, conns, tag)
                now = time.monotonic()
                stopping = self._stop.is_set()
                if self._pending and (
                    stopping
                    or now - last_flush >= self._flush_seconds
                    or self._pending_bytes >= MAX_BATCH_BYTES
                ):
                    self._flush()
                    last_flush = now
                if stopping:
                    break
            # Final drain: everything the senders already pushed into the socket
            # buffers (arc run only stops us after its children have exited).
            for conn in list(conns.values()):
                self._read(sel, conns, conn, until_empty=True)
            self._flush()
        finally:
            for conn in list(conns.values()):
                self._close_conn(sel, conns, conn)
            sel.close()
            for log in self._files.values():
                try:
                    os.close(log.fd)
                except OSError:
                    pass
            self._files.clear()

    def _accept(self, sel: selectors.BaseSelector, conns: dict[socket.socket, _Conn]) -> None:
        assert self._srv is not None
        while True:
            try:
                client, _ = self._srv.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            client.setblocking(False)
            conn = _Conn(client)
            conns[client] = conn
            sel.register(client, selectors.EVENT_READ, conn)
            self.connections += 1

    def _close_conn(
        self, sel: selectors.BaseSelector, conns: dict[socket.socket, _Conn], conn: _Conn
    ) -> None:
        if conn.buf and conn.category is not None:
            # Bytes after the last newline: the sender died mid-line. Not written.
            self.discarded_partial_bytes += len(conn.buf)
        conn.buf = b""
        conns.pop(conn.sock, None)
        try:
            sel.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        try:
            conn.sock.close()
        except OSError:
            pass

    def _reject(
        self, sel: selectors.BaseSelector, conns: dict[socket.socket, _Conn], conn: _Conn
    ) -> None:
        self.rejected += 1
        conn.buf = b""
        self._close_conn(sel, conns, conn)

    def _read(
        self,
        sel: selectors.BaseSelector,
        conns: dict[socket.socket, _Conn],
        conn: _Conn,
        *,
        until_empty: bool = False,
    ) -> None:
        while True:
            try:
                data = conn.sock.recv(RECV_BYTES)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self._close_conn(sel, conns, conn)
                return
            if not data:  # sender closed
                self._close_conn(sel, conns, conn)
                return
            if not self._ingest(sel, conns, conn, data):
                return
            if not until_empty:
                return

    def _ingest(
        self,
        sel: selectors.BaseSelector,
        conns: dict[socket.socket, _Conn],
        conn: _Conn,
        data: bytes,
    ) -> bool:
        """Returns False if the connection was closed."""
        buf = conn.buf + data
        if conn.category is None:
            nl = buf.find(b"\n")
            if nl < 0:
                if len(buf) > _HANDSHAKE_MAX:
                    self._reject(sel, conns, conn)
                    return False
                conn.buf = buf
                return True
            header, buf = buf[:nl], buf[nl + 1 :]
            if not (header.startswith(b"CAT ") and _CATEGORY_RE.match(header[4:])):
                self._reject(sel, conns, conn)
                return False
            conn.category = header[4:].decode("ascii")
        cut = buf.rfind(b"\n")
        if cut < 0:
            if len(buf) > MAX_LINE_BYTES:
                self._reject(sel, conns, conn)
                return False
            conn.buf = buf
            return True
        chunk, conn.buf = buf[: cut + 1], buf[cut + 1 :]
        self._pending.setdefault(conn.category, []).append(chunk)
        self._pending_bytes += len(chunk)
        return True

    # ------------------------------------------------------------------ #
    # writing + rotation (this thread is the only one that ever does either)
    # ------------------------------------------------------------------ #
    def _flush(self) -> None:
        pending, self._pending, self._pending_bytes = self._pending, {}, 0
        for category, chunks in pending.items():
            blob = chunks[0] if len(chunks) == 1 else b"".join(chunks)
            self._append(category, blob)

    def _path(self, category: str) -> Path:
        return self.logs_dir / f"{category}.jsonl"

    def _open(self, category: str) -> _OpenLog:
        path = self._path(category)
        log = self._files.get(category)
        if log is not None:
            # Someone outside (a CLI command with its own handler, logrotate, a
            # person) renamed or removed the file: follow the path, not the inode.
            try:
                same = os.stat(path).st_ino == log.ino
            except FileNotFoundError:
                same = False
            if not same:
                os.close(log.fd)
                del self._files[category]
                log = None
        if log is None:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, permissions.SHARED_FILE_MODE)
            st = os.fstat(fd)
            log = self._files[category] = _OpenLog(fd, st.st_size, st.st_ino)
        return log

    def _append(self, category: str, blob: bytes) -> None:
        try:
            log = self._open(category)
            view = memoryview(blob)
            while view:  # a short write is legal; finish the batch
                written = os.write(log.fd, view)
                view = view[written:]
        except OSError as exc:
            self.write_errors += 1
            self._report(f"cannot write logs/{category}.jsonl: {exc}")
            return
        log.size += len(blob)
        self.writes += 1
        self.lines_written += blob.count(b"\n")
        if log.size >= self._max_bytes:
            self._rotate(category, log)

    def _rotate(self, category: str, log: _OpenLog) -> None:
        path = self._path(category)
        os.close(log.fd)
        del self._files[category]
        try:
            if self._backup_count > 0:
                for i in range(self._backup_count - 1, 0, -1):
                    src = f"{path}.{i}"
                    if os.path.exists(src):
                        os.replace(src, f"{path}.{i + 1}")
                os.replace(path, f"{path}.1")
            else:
                os.unlink(path)
            self.rotations += 1
        except OSError as exc:
            self.write_errors += 1
            self._report(f"cannot rotate logs/{category}.jsonl: {exc}")

    def _report(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_error_report >= 5.0:
            self._last_error_report = now
            print(f"arc log collector: {message}", file=sys.stderr, flush=True)
