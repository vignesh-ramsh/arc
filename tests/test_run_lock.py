"""Regression coverage for the `arc run` singleton lock (arc.events.
acquire_run_lock/release_run_lock/RunLockError) — added so a second `arc
run` invocation can never start alongside a still-live first one for the
same project. The actual incident this closes: a first `arc run` that got
stopped (Ctrl-Z) rather than killed sat alive-but-unresponsive, holding
port 8001, while a second `arc run` was launched on top of it with no
warning at all.

Tested directly against the lock primitives (no subprocess/CLI plumbing)
— that's the level the actual logic lives at, and it's fully hermetic:
no real gateway/lineup process ever needs to exist for these."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from arc.events import RunLockError, acquire_run_lock, release_run_lock

_LOCK_PATH = ".arc/runtime/arc_run.lock"


class TestAcquireRunLock:
    def test_first_acquire_succeeds_and_writes_the_current_pid(self, tmp_path: Path):
        path = acquire_run_lock(tmp_path)
        assert path == tmp_path / _LOCK_PATH
        assert path.is_file()
        assert json.loads(path.read_text())["pid"] == os.getpid()

    def test_a_second_acquire_from_the_same_process_is_not_a_conflict(self, tmp_path: Path):
        """Re-entrant — the SAME process re-acquiring (e.g. a retry) must
        never lock itself out."""
        acquire_run_lock(tmp_path)
        acquire_run_lock(tmp_path)  # must not raise

    def test_the_runtime_directory_is_created_if_missing(self, tmp_path: Path):
        assert not (tmp_path / ".arc" / "runtime").exists()
        acquire_run_lock(tmp_path)
        assert (tmp_path / ".arc" / "runtime").is_dir()


class TestSecondAcquireWhileFirstIsLive:
    def test_raises_naming_the_live_holders_pid(self, tmp_path: Path):
        lock_path = tmp_path / _LOCK_PATH
        lock_path.parent.mkdir(parents=True)
        # A genuinely different, currently-alive pid: this test process's
        # own parent (the pytest runner), real and alive for the whole
        # test, and guaranteed != os.getpid().
        other_pid = os.getppid()
        lock_path.write_text(json.dumps({"pid": other_pid, "started_at": 0}))

        with pytest.raises(RunLockError, match=str(other_pid)):
            acquire_run_lock(tmp_path)

    def test_the_lock_file_is_left_untouched_on_conflict(self, tmp_path: Path):
        lock_path = tmp_path / _LOCK_PATH
        lock_path.parent.mkdir(parents=True)
        other_pid = os.getppid()
        lock_path.write_text(json.dumps({"pid": other_pid, "started_at": 123}))

        with pytest.raises(RunLockError):
            acquire_run_lock(tmp_path)

        # not overwritten by the failed acquire attempt
        assert json.loads(lock_path.read_text()) == {"pid": other_pid, "started_at": 123}


class TestStaleLockIsSilentlyReclaimed:
    def test_a_dead_pid_does_not_block_a_new_acquire(self, tmp_path: Path):
        lock_path = tmp_path / _LOCK_PATH
        lock_path.parent.mkdir(parents=True)
        # Far past any real pid_max — guaranteed to never be alive.
        lock_path.write_text(json.dumps({"pid": 999_999_999, "started_at": 0}))

        acquire_run_lock(tmp_path)  # must not raise — reclaimed, not a conflict

        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_a_corrupted_lock_file_is_also_treated_as_stale(self, tmp_path: Path):
        lock_path = tmp_path / _LOCK_PATH
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("not valid json{{{")

        acquire_run_lock(tmp_path)  # must not raise

        assert json.loads(lock_path.read_text())["pid"] == os.getpid()

    def test_a_lock_file_missing_the_pid_key_is_also_treated_as_stale(self, tmp_path: Path):
        lock_path = tmp_path / _LOCK_PATH
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text(json.dumps({"started_at": 0}))

        acquire_run_lock(tmp_path)  # must not raise


class TestReleaseRunLock:
    def test_removes_the_lock_file(self, tmp_path: Path):
        path = acquire_run_lock(tmp_path)
        assert path.is_file()
        release_run_lock(tmp_path)
        assert not path.is_file()

    def test_is_a_safe_no_op_when_no_lock_was_ever_acquired(self, tmp_path: Path):
        release_run_lock(tmp_path)  # must not raise

    def test_after_release_a_fresh_acquire_succeeds_cleanly(self, tmp_path: Path):
        acquire_run_lock(tmp_path)
        release_run_lock(tmp_path)
        acquire_run_lock(tmp_path)  # must not raise
