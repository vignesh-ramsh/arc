"""Who else on the machine can read `.arc/` (arc/permissions.py, arc/store.py, `arc init`,
`arc doctor --fix`).

A project's `.arc/` holds the settings store - every plain setting (the /metrics token,
`restart_command`, ...) and the encrypted secrets - and the master key that decrypts them. New
projects created it 775 with the store 644, readable by every local user. Now a new project is
private from the first byte; an old one is repaired by `arc doctor --fix`, and never silently.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arc import cli, permissions, store
from arc.settings import SettingsManager


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(params=[0o000, 0o002, 0o022], ids=["umask000", "umask002", "umask022"])
def umask(request):
    """The umask the user happens to have: the result must not depend on it."""
    previous = os.umask(request.param)
    yield request.param
    os.umask(previous)


@pytest.fixture(autouse=True)
def fresh_store_cache():
    store._connections.clear()
    yield
    store._connections.clear()


class TestCreatingPrivately:
    def test_a_new_directory_is_0700_whatever_the_umask(self, tmp_path, umask):
        assert permissions.create_private_dir(tmp_path / ".arc") is True
        assert mode(tmp_path / ".arc") == 0o700

    def test_even_an_odd_umask_cannot_leave_the_owner_without_access(self, tmp_path):
        previous = os.umask(0o277)  # masks the owner's own bits, which mkdir's mode alone would obey
        try:
            permissions.create_private_dir(tmp_path / ".arc")
        finally:
            os.umask(previous)
        assert mode(tmp_path / ".arc") == 0o700

    def test_an_existing_directory_is_left_exactly_as_it_is(self, tmp_path):
        d = tmp_path / ".arc"
        d.mkdir()
        d.chmod(0o775)
        assert permissions.create_private_dir(d) is False
        assert mode(d) == 0o775

    def test_a_new_file_is_0600_whatever_the_umask(self, tmp_path, umask):
        assert permissions.create_private_file(tmp_path / "f") is True
        assert mode(tmp_path / "f") == 0o600

    def test_an_existing_file_is_left_alone(self, tmp_path):
        f = tmp_path / "f"
        f.write_text("keep")
        f.chmod(0o644)
        assert permissions.create_private_file(f) is False
        assert mode(f) == 0o644 and f.read_text() == "keep"

    def test_a_written_private_file_never_had_a_looser_mode(self, tmp_path, umask):
        modes_seen = []
        real_open = os.open

        def spy(path, flags, mode_=0o777, *a, **k):
            modes_seen.append(mode_)
            return real_open(path, flags, mode_, *a, **k)

        os.open = spy
        try:
            permissions.write_private_file(tmp_path / "key", "secret")
        finally:
            os.open = real_open
        assert modes_seen == [0o600] and mode(tmp_path / "key") == 0o600
        assert (tmp_path / "key").read_text() == "secret"

    def test_it_never_overwrites(self, tmp_path):
        (tmp_path / "key").write_text("old")
        with pytest.raises(FileExistsError):
            permissions.write_private_file(tmp_path / "key", "new")
        assert (tmp_path / "key").read_text() == "old"


class TestTheStore:
    def test_a_new_store_and_its_wal_files_are_private(self, tmp_path, umask):
        arc_dir = tmp_path / ".arc"
        conn = store.open_db(arc_dir / "arc.store.db")
        conn.execute("INSERT INTO setting (key, value, is_secret, updated_at) VALUES ('k', 'v', 0, 'now')")
        conn.commit()
        assert mode(arc_dir) == 0o700
        names = sorted(p.name for p in arc_dir.iterdir())
        assert names == ["arc.store.db", "arc.store.db-shm", "arc.store.db-wal"]  # the WAL files exist now
        assert {n: mode(arc_dir / n) for n in names} == {n: 0o600 for n in names}

    def test_an_existing_store_is_not_silently_changed(self, tmp_path):
        """A deployment may deliberately share .arc/ through a group; only `doctor --fix` changes it."""
        arc_dir = tmp_path / ".arc"
        arc_dir.mkdir()
        arc_dir.chmod(0o775)
        db = arc_dir / "arc.store.db"
        db.write_bytes(b"")
        db.chmod(0o644)
        conn = store.open_db(db)
        conn.execute("INSERT INTO setting (key, value, is_secret, updated_at) VALUES ('k', 'v', 0, 'now')")
        conn.commit()
        assert mode(arc_dir) == 0o775 and mode(db) == 0o644

    def test_the_settings_manager_creates_a_private_store(self, project):
        arc_dir = project / ".arc"
        for leftover in arc_dir.glob("arc.store.db*"):
            leftover.unlink()
        store._connections.clear()
        mgr = SettingsManager(arc_dir)
        mgr.set("some_key", "some value")
        assert mode(arc_dir / "arc.store.db") == 0o600
        assert mode(arc_dir / "arc.store.db-wal") == 0o600


class TestArcInit:
    def test_a_new_project_is_private_from_the_start(self, tmp_path, monkeypatch, umask):
        commands: list[list[str]] = []

        def fake_run(cmd, cwd):
            commands.append(cmd)
            if cmd[:2] == ["git", "clone"]:
                Path(cmd[-1]).mkdir(parents=True)

        monkeypatch.setattr(cli, "run", fake_run)
        project = tmp_path / "shop"
        result = CliRunner().invoke(cli.app, ["init", str(project), "--kernel-repo", "https://example.invalid/arc.git"])
        assert result.exit_code == 0, result.output
        arc_dir = project / ".arc"
        assert mode(arc_dir) == 0o700
        assert mode(arc_dir / "arc.mkey") == 0o600 and len(( arc_dir / "arc.mkey").read_text()) == 64
        assert mode(arc_dir / "arc.store.db") == 0o600
        assert mode(arc_dir / "arc.store.db-wal") == 0o600
        assert permissions.audit(arc_dir) == []

    def test_running_init_again_leaves_the_mode_of_an_existing_arc_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "run", lambda cmd, cwd: Path(cmd[-1]).mkdir(parents=True, exist_ok=True) if cmd[:2] == ["git", "clone"] else None)
        project = tmp_path / "shop"
        (project / ".arc").mkdir(parents=True)
        (project / ".arc").chmod(0o775)
        assert CliRunner().invoke(cli.app, ["init", str(project), "--kernel-repo", "x"]).exit_code == 0
        assert mode(project / ".arc") == 0o775


def loose_project(tmp_path: Path) -> Path:
    """What every project created before this looked like."""
    arc_dir = tmp_path / ".arc"
    arc_dir.mkdir()
    arc_dir.chmod(0o775)
    for name, content in {"arc.mkey": "0" * 64, "arc.store.db": "", "arc.store.db-wal": "", "arc.store.db-shm": ""}.items():
        (arc_dir / name).write_text(content)
        (arc_dir / name).chmod(0o644)
    return arc_dir


class TestAudit:
    def test_a_private_project_has_nothing_to_report(self, tmp_path):
        arc_dir = tmp_path / ".arc"
        permissions.create_private_dir(arc_dir)
        permissions.write_private_file(arc_dir / "arc.mkey", "0" * 64)
        permissions.create_private_file(arc_dir / "arc.store.db")
        assert permissions.audit(arc_dir) == []

    def test_an_old_project_is_reported_directory_first(self, tmp_path):
        arc_dir = loose_project(tmp_path)
        found = permissions.audit(arc_dir)
        assert [f.path.name for f in found] == [".arc", "arc.mkey", "arc.store.db", "arc.store.db-wal", "arc.store.db-shm"]
        assert [f.kind for f in found] == ["directory"] + ["file"] * 4
        assert (found[0].mode, found[0].wanted) == (0o775, 0o700) and (found[1].mode, found[1].wanted) == (0o644, 0o600)
        assert all(f.fixable for f in found)

    @pytest.mark.parametrize("loose", [0o640, 0o604, 0o660, 0o666, 0o644])
    def test_any_access_for_group_or_others_counts(self, tmp_path, loose):
        arc_dir = tmp_path / ".arc"
        permissions.create_private_dir(arc_dir)
        (arc_dir / "arc.mkey").write_text("k")
        (arc_dir / "arc.mkey").chmod(loose)
        (found,) = permissions.audit(arc_dir)
        assert found.path.name == "arc.mkey" and found.mode == loose

    def test_owner_only_modes_other_than_the_default_are_fine(self, tmp_path):
        arc_dir = tmp_path / ".arc"
        permissions.create_private_dir(arc_dir)
        (arc_dir / "arc.mkey").write_text("k")
        (arc_dir / "arc.mkey").chmod(0o400)
        assert permissions.audit(arc_dir) == []

    def test_files_that_do_not_exist_are_not_reported(self, tmp_path):
        arc_dir = tmp_path / ".arc"
        arc_dir.mkdir()
        arc_dir.chmod(0o700)
        assert permissions.audit(arc_dir) == []
        assert permissions.audit(tmp_path / "no-such-dir") == []

    def test_something_owned_by_someone_else_is_reported_as_not_fixable_by_us(self, tmp_path, monkeypatch):
        arc_dir = loose_project(tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: os.stat(arc_dir).st_uid + 1)
        assert not any(f.fixable for f in permissions.audit(arc_dir))

    def test_root_can_fix_anything(self, tmp_path, monkeypatch):
        arc_dir = loose_project(tmp_path)
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert all(f.fixable for f in permissions.audit(arc_dir))


class TestFix:
    def test_modes_are_set_and_a_second_run_finds_nothing(self, tmp_path):
        arc_dir = loose_project(tmp_path)
        fixed, failed = permissions.fix(permissions.audit(arc_dir))
        assert len(fixed) == 5 and failed == []
        assert mode(arc_dir) == 0o700 and mode(arc_dir / "arc.store.db") == 0o600
        assert permissions.audit(arc_dir) == []

    def test_what_cannot_be_changed_is_reported_with_the_reason(self, tmp_path, monkeypatch):
        arc_dir = loose_project(tmp_path)
        real_chmod = Path.chmod

        def refuse_the_key(self, m, **kw):
            if self.name == "arc.mkey":
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(self, m, **kw)

        monkeypatch.setattr(Path, "chmod", refuse_the_key)
        fixed, failed = permissions.fix(permissions.audit(arc_dir))
        assert [f.path.name for f in fixed] == [".arc", "arc.store.db", "arc.store.db-wal", "arc.store.db-shm"]
        ((finding, reason),) = failed
        assert finding.path.name == "arc.mkey" and "not permitted" in reason


class TestDoctor:
    @pytest.fixture
    def old_project(self, project, monkeypatch):
        monkeypatch.chdir(project)
        arc_dir = project / ".arc"
        arc_dir.chmod(0o775)
        for name in ("arc.mkey",):
            (arc_dir / name).chmod(0o644)
        (arc_dir / "arc.store.db").write_bytes(b"")
        (arc_dir / "arc.store.db").chmod(0o644)
        return project

    def doctor(self, *args):
        return CliRunner().invoke(cli.app, ["doctor", *args])

    def test_a_plain_doctor_warns_and_changes_nothing(self, old_project):
        result = self.doctor()
        assert result.exit_code == 0, result.output
        out = " ".join(result.output.split())
        assert "warning:" in out and ".arc is mode 775" in out and "arc.mkey is mode 644" in out
        assert "other users on this machine can read it" in out and "arc doctor --fix" in out
        assert mode(old_project / ".arc") == 0o775 and mode(old_project / ".arc" / "arc.mkey") == 0o644

    def test_fix_repairs_it_and_says_what_it_did(self, old_project):
        result = self.doctor("--fix")
        assert result.exit_code == 0, result.output
        out = " ".join(result.output.split())
        assert "fixed:" in out and "775 -> 700" in out and "644 -> 600" in out
        assert mode(old_project / ".arc") == 0o700 and mode(old_project / ".arc" / "arc.mkey") == 0o600
        assert " is mode " not in result.output  # no permissions warning left

    def test_a_private_project_says_so(self, old_project):
        self.doctor("--fix")
        assert "closed to other users" in " ".join(self.doctor().output.split())

    def test_json_reports_what_was_exposed_and_what_was_fixed(self, old_project):
        before = json.loads(self.doctor("--json").output)["permissions"]
        assert {f["path"].rsplit("/", 1)[-1] for f in before["exposed"]} == {".arc", "arc.mkey", "arc.store.db"}
        assert before["fixed"] == [] and before["failed"] == []
        after = json.loads(self.doctor("--json", "--fix").output)["permissions"]
        assert len(after["fixed"]) == 3 and after["failed"] == []
        assert json.loads(self.doctor("--json").output)["permissions"]["exposed"] == []

    def test_json_fix_that_cannot_be_applied_lists_it_and_exits_1(self, old_project, monkeypatch):
        real_chmod = Path.chmod

        def refuse(self, m, **kw):
            if self.name == "arc.mkey":
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(self, m, **kw)

        monkeypatch.setattr(Path, "chmod", refuse)
        result = self.doctor("--json", "--fix")
        assert result.exit_code == 1
        (failed,) = json.loads(result.output)["permissions"]["failed"]
        assert failed["path"].endswith("arc.mkey") and "not permitted" in failed["reason"]

    def test_files_owned_by_someone_else_are_pointed_at_the_right_user(self, old_project, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: os.stat(old_project / ".arc").st_uid + 1)
        out = " ".join(self.doctor().output.split())
        assert "owned by another user" in out and "arc doctor --fix` as that user" in out
        assert "Run `arc doctor --fix` to set them." not in out

    def test_a_fix_that_cannot_be_applied_exits_1(self, old_project, monkeypatch):
        real_chmod = Path.chmod

        def refuse(self, m, **kw):
            if self.name == "arc.mkey":
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(self, m, **kw)

        monkeypatch.setattr(Path, "chmod", refuse)
        result = self.doctor("--fix")
        assert result.exit_code == 1 and "could not fix" in result.output and "arc.mkey" in result.output


def old_style_project(tmp_path: Path) -> Path:
    """A project as `arc init` made it before backups, logs and uploads were closed: the umask
    default everywhere (folders 775, files 664/644)."""
    root = tmp_path
    for rel in (".arc", "backups", "backups/db", "backups/full", "logs", "files", "files/public", "files/private"):
        (root / rel).mkdir(parents=True, exist_ok=True)
        (root / rel).chmod(0o775)
    for rel in ("backups/db/20260101.dump", "backups/full/arc-backup-x.tar", "logs/gateway.jsonl", "logs/system.jsonl.1"):
        (root / rel).write_text("x")
        (root / rel).chmod(0o644)
    (root / ".arc" / "arc.mkey").write_text("0" * 64)
    (root / ".arc" / "arc.mkey").chmod(0o600)
    (root / ".arc").chmod(0o700)
    (root / "backups" / "db" / ".gitkeep").touch()
    return root


def names(findings, root: Path) -> list[str]:
    return sorted(str(f.path.relative_to(root)) for f in findings)


class TestTheWholeProject:
    def test_what_is_exposed_beyond_arc_is_found(self, tmp_path):
        root = old_style_project(tmp_path)
        assert names(permissions.audit_project(root), root) == sorted([
            "backups", "backups/db", "backups/db/20260101.dump", "backups/full",
            "backups/full/arc-backup-x.tar", "logs", "logs/gateway.jsonl", "logs/system.jsonl.1",
            "files", "files/private", "files/public",
        ])

    def test_backups_are_wanted_private_and_logs_and_uploads_closed_to_others_only(self, tmp_path):
        root = old_style_project(tmp_path)
        wanted = {str(f.path.relative_to(root)): f.wanted for f in permissions.audit_project(root)}
        assert wanted["backups/db/20260101.dump"] == 0o600 and wanted["backups/db"] == 0o700
        assert wanted["logs/gateway.jsonl"] == 0o640 and wanted["logs"] == 0o750
        assert wanted["files"] == 0o750 and wanted["files/public"] == 0o750

    def test_a_group_may_keep_reading_logs_and_uploads_but_not_backups(self, tmp_path):
        root = old_style_project(tmp_path)
        (root / "logs").chmod(0o750)
        (root / "logs" / "gateway.jsonl").chmod(0o640)
        (root / "logs" / "system.jsonl.1").chmod(0o640)
        (root / "files").chmod(0o750)
        for sub in ("public", "private"):
            (root / "files" / sub).chmod(0o750)
        (root / "backups" / "db" / "20260101.dump").chmod(0o640)  # group-readable backup: still a finding
        found = names(permissions.audit_project(root), root)
        assert "logs" not in found and "files" not in found and "logs/gateway.jsonl" not in found
        assert "backups/db/20260101.dump" in found

    def test_gitkeep_markers_are_not_reported(self, tmp_path):
        root = old_style_project(tmp_path)
        (root / "backups" / "db" / ".gitkeep").chmod(0o666)
        assert not [f for f in permissions.audit_project(root) if f.path.name == ".gitkeep"]

    def test_symlinks_are_never_followed(self, tmp_path):
        root = old_style_project(tmp_path)
        outside = tmp_path.parent / "outside-target"
        outside.write_text("x")
        outside.chmod(0o666)
        (root / "backups" / "db" / "link.dump").symlink_to(outside)
        assert not [f for f in permissions.audit_project(root) if f.path.name == "link.dump"]
        assert oct(outside.stat().st_mode & 0o777) == "0o666"

    def test_a_missing_folder_is_not_a_finding(self, tmp_path):
        root = tmp_path
        permissions.create_private_dir(root / ".arc")
        assert permissions.audit_project(root) == []

    def test_fix_repairs_every_one_and_a_second_audit_is_clean(self, tmp_path):
        root = old_style_project(tmp_path)
        fixed, failed = permissions.fix(permissions.audit_project(root))
        assert failed == [] and len(fixed) == 11
        assert permissions.audit_project(root) == []
        assert mode(root / "backups" / "db" / "20260101.dump") == 0o600
        assert mode(root / "logs" / "gateway.jsonl") == 0o640 and mode(root / "logs") == 0o750

    def test_the_default_audit_of_arc_alone_is_unchanged(self, tmp_path):
        root = old_style_project(tmp_path)
        assert permissions.audit(root / ".arc") == []


class TestCreatingTheRestPrivately:
    def test_a_directory_can_be_created_with_another_closed_mode(self, tmp_path, umask):
        assert permissions.create_private_dir(tmp_path / "logs", permissions.SHARED_DIR_MODE) is True
        assert mode(tmp_path / "logs") == 0o750

    def test_ensure_private_file_makes_a_new_file_0600(self, tmp_path, umask):
        permissions.ensure_private_file(tmp_path / "dump")
        assert mode(tmp_path / "dump") == 0o600

    def test_ensure_private_file_tightens_an_existing_one_without_emptying_it(self, tmp_path):
        f = tmp_path / "dump"
        f.write_text("rows")
        f.chmod(0o666)
        permissions.ensure_private_file(f)
        assert mode(f) == 0o600 and f.read_text() == "rows"

    def test_init_closes_backups_and_logs_whatever_the_umask(self, tmp_path, monkeypatch, umask):
        def fake_run(cmd, cwd):
            if cmd[:2] == ["git", "clone"]:
                Path(cmd[-1]).mkdir(parents=True)

        monkeypatch.setattr(cli, "run", fake_run)
        project = tmp_path / "shop"
        result = CliRunner().invoke(cli.app, ["init", str(project), "--kernel-repo", "https://example.invalid/arc.git"])
        assert result.exit_code == 0, result.output
        assert mode(project / "logs") == 0o750
        for d in ("backups", "backups/db", "backups/files"):
            assert mode(project / d) == 0o700
        assert permissions.audit_project(project) == []

    def test_log_files_are_created_closed_to_other_users(self, tmp_path, umask):
        import logging

        from arc.log import _SharedModeRotatingFileHandler

        handler = _SharedModeRotatingFileHandler(str(tmp_path / "x.jsonl"), maxBytes=50, backupCount=2)
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "a message long enough to rotate " * 3, (), None)
        for _ in range(3):  # past maxBytes, so the file is rotated and a fresh one is created
            handler.emit(record)
        handler.close()
        assert (tmp_path / "x.jsonl.1").exists()
        assert not mode(tmp_path / "x.jsonl") & 0o007 and not mode(tmp_path / "x.jsonl.1") & 0o007
        assert mode(tmp_path / "x.jsonl") & 0o600 == 0o600


class TestCreatingTheSameFolderTwice:
    def test_two_creators_racing_for_a_new_folder_both_succeed(self, tmp_path):
        """Every process creates logs/ at boot; two uploads can need the same new folder. Making a
        folder that appeared a moment ago is not an error."""
        import threading

        target = tmp_path / "logs"
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def create():
            try:
                barrier.wait()
                permissions.create_private_dir(target, permissions.SHARED_DIR_MODE)
            except BaseException as exc:  # noqa: BLE001 - the point is that none escapes
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert mode(target) == 0o750

    def test_only_one_of_two_sequential_creators_reports_having_made_it(self, tmp_path):
        assert permissions.create_private_dir(tmp_path / "d") is True
        assert permissions.create_private_dir(tmp_path / "d") is False
