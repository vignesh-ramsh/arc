"""Regression coverage for arc.events.register_process()/list_processes()'s
pruning of dead entries — specifically that a pid whose NUMBER has been
reused by an unrelated process (after the original one that registered it
exited) is correctly pruned as stale rather than kept as though the
original process were still alive. Same underlying fix, same incident, as
test_run_lock.py's own TestPidReuseIsNotMistakenForTheOriginalHolder —
covered separately here because list_processes() is a second, independent
call site of the same _process_matches() check, not because the logic
itself differs.

Tested directly against the registry primitives (no real gateway/lineup
process ever needs to exist for these), same posture as test_run_lock.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

from arc.events import _pid_start_time, list_processes, register_process

_PROCESSES_DIR = ".arc/runtime/processes"


class TestListProcesses:
    def test_a_freshly_registered_process_is_listed(self, tmp_path: Path):
        register_process(tmp_path, role="gateway")
        entries = list_processes(tmp_path)
        assert [e["pid"] for e in entries] == [os.getpid()]
        assert entries[0]["role"] == "gateway"

    def test_registering_records_this_processs_own_start_time(self, tmp_path: Path):
        path = register_process(tmp_path, role="gateway")
        assert json.loads(path.read_text())["start_time"] == _pid_start_time(os.getpid())

    def test_a_definitely_dead_pid_is_pruned(self, tmp_path: Path):
        directory = tmp_path / _PROCESSES_DIR
        directory.mkdir(parents=True)
        entry_path = directory / "999999999.json"
        entry_path.write_text(json.dumps({"pid": 999_999_999, "role": "gateway"}))

        assert list_processes(tmp_path) == []
        assert not entry_path.exists()  # prune=True by default

    def test_a_live_pid_with_a_mismatched_start_time_is_pruned(self, tmp_path: Path):
        """The actual incident: a registered pid still exists, but the OS
        has since reused that pid number for an unrelated process — a
        bare "is something alive at this pid" check can't tell, so it
        must be the start_time cross-check that catches it."""
        directory = tmp_path / _PROCESSES_DIR
        directory.mkdir(parents=True)
        other_pid = os.getppid()
        real_start_time = _pid_start_time(other_pid)
        assert real_start_time is not None
        entry_path = directory / f"{other_pid}.json"
        entry_path.write_text(
            json.dumps({"pid": other_pid, "role": "gateway", "start_time": real_start_time + 1})
        )

        assert list_processes(tmp_path) == []
        assert not entry_path.exists()

    def test_a_live_pid_with_a_matching_start_time_is_kept(self, tmp_path: Path):
        directory = tmp_path / _PROCESSES_DIR
        directory.mkdir(parents=True)
        other_pid = os.getppid()
        real_start_time = _pid_start_time(other_pid)
        assert real_start_time is not None
        entry_path = directory / f"{other_pid}.json"
        entry_path.write_text(
            json.dumps({"pid": other_pid, "role": "gateway", "start_time": real_start_time})
        )

        entries = list_processes(tmp_path)
        assert [e["pid"] for e in entries] == [other_pid]
        assert entry_path.exists()

    def test_a_live_pid_with_no_recorded_start_time_falls_back_to_the_old_alive_check(
        self, tmp_path: Path
    ):
        directory = tmp_path / _PROCESSES_DIR
        directory.mkdir(parents=True)
        other_pid = os.getppid()
        entry_path = directory / f"{other_pid}.json"
        entry_path.write_text(json.dumps({"pid": other_pid, "role": "gateway"}))

        entries = list_processes(tmp_path)
        assert [e["pid"] for e in entries] == [other_pid]

    def test_prune_false_leaves_stale_entries_on_disk(self, tmp_path: Path):
        directory = tmp_path / _PROCESSES_DIR
        directory.mkdir(parents=True)
        entry_path = directory / "999999999.json"
        entry_path.write_text(json.dumps({"pid": 999_999_999, "role": "gateway"}))

        assert list_processes(tmp_path, prune=False) == []
        assert entry_path.exists()  # not deleted this time

    def test_no_processes_dir_yet_returns_empty(self, tmp_path: Path):
        assert list_processes(tmp_path) == []
