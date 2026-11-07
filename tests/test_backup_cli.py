"""`arc backup create | verify | restore` as the operator runs them (arc/backupcmd.py).

The database tools are replaced by a stand-in recording what they were asked to do, so what is
checked here is the command's behaviour: which database a restore will and will not touch, what is
asked for, what is reported, and that failures are messages rather than tracebacks. The real
pg_dump / pg_restore round trip is in tests/integration/test_backup_restore_drill.py.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arc import backupcmd, cli
from arc.settings import SettingsManager

PASSPHRASE = "correct horse battery staple"
DSN = "postgresql://shop:pw@db.internal/shopdb"
NEW_DSN = "postgresql://shop:pw@restore.internal/restoredb"


class FakePgdump:
    class DumpError(RuntimeError):
        pass

    def __init__(self):
        self.calls: list[tuple] = []
        self.target_is_empty = True
        self.missing_tools: list[str] = []
        self.fail_dump = False
        self.fail_restore = False

    def require_tools(self, *names):
        for name in names:
            if name in self.missing_tools:
                raise self.DumpError(f"`{name}` not found on PATH")

    def connection_args(self, dsn):
        return ["-h", "x"], dsn.rsplit("/", 1)[1], {}

    def dump(self, dsn, path):
        self.calls.append(("dump", dsn))
        if self.fail_dump:
            raise self.DumpError("pg_dump failed (exit 1): permission denied for table _users")
        path.write_bytes(b"PGDMP-fake")

    def restore(self, dsn, path, *, replace, portable):
        self.calls.append(("restore", dsn, replace, portable, path.read_bytes()))
        if self.fail_restore:
            raise self.DumpError("pg_restore failed (exit 1): relation already exists")

    def describe(self, path):
        self.calls.append(("describe", path.name))
        return "toc"

    def is_empty(self, dsn):
        self.calls.append(("is_empty", dsn))
        return self.target_is_empty


@pytest.fixture
def pg(monkeypatch) -> FakePgdump:
    fake = FakePgdump()
    monkeypatch.setattr(backupcmd, "_pgdump", lambda: fake)
    return fake


@pytest.fixture
def instance(project: Path, monkeypatch, pg) -> Path:
    monkeypatch.chdir(project)
    monkeypatch.delenv("ARC_BACKUP_PASSPHRASE", raising=False)
    mgr = SettingsManager(project / ".arc")
    mgr.set("pgdb_dsn", DSN, secret=True)
    mgr.declare("api_token", secret=True)
    mgr.set("api_token", "s3cret", secret=True)
    (project / ".arc" / "plugins.lock").write_text('[plugins.pgdb]\nversion = "0.1.0"\n')
    (project / "files").mkdir()
    (project / "files" / "a.txt").write_text("uploaded")
    (project / ".gitignore").write_text("logs/*\n")
    return project


@pytest.fixture
def passfile(tmp_path) -> Path:
    path = tmp_path / "pass.txt"
    path.write_text(PASSPHRASE + "\n")
    return path


def run(*args, input=None):
    return CliRunner().invoke(cli.app, ["backup", *args], input=input)


def flat(result) -> str:
    return " ".join(result.output.split())


def archives(project: Path) -> list[Path]:
    return sorted((project / "backups" / "full").glob("arc-backup-*.tar"))


class TestCreate:
    def test_writes_one_archive_with_the_database_the_files_and_the_key(self, instance, pg, passfile):
        result = run("create", "--passphrase-file", str(passfile))
        assert result.exit_code == 0, result.output
        (archive,) = archives(instance)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            assert tar.extractfile("database.dump").read() == b"PGDMP-fake"
        assert {"key.enc", "state/arc.store.db", "files/filer/a.txt", "manifest.json"} <= set(names)
        assert ("dump", DSN) in pg.calls
        assert "Backup written" in flat(result) and "database: yes (shopdb)" in flat(result)
        # the label is shown: Rich reads a bare [filer] as markup and prints nothing in its place
        assert "files [filer]: 1 (" in flat(result)

    def test_the_passphrase_can_come_from_the_environment(self, instance, monkeypatch):
        monkeypatch.setenv("ARC_BACKUP_PASSPHRASE", PASSPHRASE)
        assert run("create").exit_code == 0 and len(archives(instance)) == 1

    def test_or_be_asked_for_twice(self, instance, monkeypatch):
        answers = iter([PASSPHRASE, PASSPHRASE])
        monkeypatch.setattr(backupcmd.getpass, "getpass", lambda prompt="": next(answers))
        assert run("create").exit_code == 0

    def test_two_different_answers_are_refused(self, instance, monkeypatch):
        answers = iter([PASSPHRASE, "something else entirely"])
        monkeypatch.setattr(backupcmd.getpass, "getpass", lambda prompt="": next(answers))
        result = run("create")
        assert result.exit_code == 1 and "differ" in flat(result) and archives(instance) == []

    def test_a_short_passphrase_is_refused(self, instance, tmp_path):
        short = tmp_path / "short.txt"
        short.write_text("tooshort")
        result = run("create", "--passphrase-file", str(short))
        assert result.exit_code == 1 and "at least 12" in flat(result) and archives(instance) == []

    def test_an_unreadable_passphrase_file_is_a_message(self, instance, tmp_path):
        result = run("create", "--passphrase-file", str(tmp_path / "missing.txt"))
        assert result.exit_code == 1 and "cannot read the passphrase file" in flat(result)

    def test_a_backup_directory_in_the_project_is_kept_out_of_git(self, instance, passfile):
        result = run("create", "--passphrase-file", str(passfile))
        assert "backups/full/*" in (instance / ".gitignore").read_text() and ".gitignore" in flat(result)

    def test_without_a_database_configured_it_says_so_and_still_works(self, instance, pg, passfile):
        SettingsManager(instance / ".arc").delete("pgdb_dsn")
        result = run("create", "--passphrase-file", str(passfile))
        assert result.exit_code == 0 and "no database configured" in flat(result) and not pg.calls

    def test_no_database_and_no_files_leave_them_out(self, instance, pg, passfile):
        assert run("create", "--passphrase-file", str(passfile), "--no-database", "--no-files").exit_code == 0
        with tarfile.open(archives(instance)[0]) as tar:
            assert "database.dump" not in tar.getnames() and not any(n.startswith("files/") for n in tar.getnames())

    def test_the_dumping_tool_missing_is_a_message(self, instance, pg, passfile):
        pg.missing_tools = ["pg_dump"]
        result = run("create", "--passphrase-file", str(passfile))
        assert result.exit_code == 1 and "pg_dump" in flat(result) and archives(instance) == []

    def test_a_failing_dump_is_a_message_and_leaves_no_archive(self, instance, pg, passfile):
        pg.fail_dump = True
        result = run("create", "--passphrase-file", str(passfile))
        assert result.exit_code == 1 and "permission denied" in flat(result) and "Traceback" not in result.output
        assert archives(instance) == [] and not list((instance / "backups" / "full").glob(".*"))

    def test_s3_storage_is_called_out_as_not_included(self, instance, passfile):
        SettingsManager(instance / ".arc").set("filer_default_storage", "s3")
        assert "S3" in flat(run("create", "--passphrase-file", str(passfile)))

    def test_files_in_a_custom_root_are_found(self, instance, tmp_path, passfile):
        elsewhere = tmp_path / "uploads"
        elsewhere.mkdir()
        (elsewhere / "x.pdf").write_bytes(b"%PDF")
        SettingsManager(instance / ".arc").set("filer_local_root", str(elsewhere))
        run("create", "--passphrase-file", str(passfile))
        with tarfile.open(archives(instance)[0]) as tar:
            assert "files/filer/x.pdf" in tar.getnames() and "files/filer/a.txt" not in tar.getnames()

    def test_keep_prunes_the_old_ones_but_not_the_new(self, instance, passfile):
        directory = instance / "backups" / "full"
        directory.mkdir(parents=True)
        for day in (1, 2, 3):
            (directory / f"arc-backup-shop-2020010{day}T000000Z.tar").write_text("old")
        result = run("create", "--passphrase-file", str(passfile), "--keep", "2")
        assert result.exit_code == 0 and len(archives(instance)) == 2
        assert archives(instance)[-1].stat().st_size > 100  # the new one is the one that survived

    def test_a_chosen_path_outside_the_project(self, instance, tmp_path, passfile):
        out = tmp_path / "elsewhere" / "mine.tar"
        assert run("create", "--passphrase-file", str(passfile), "--out", str(out)).exit_code == 0 and out.exists()

    def test_it_never_overwrites(self, instance, tmp_path, passfile):
        out = tmp_path / "mine.tar"
        out.write_text("precious")
        result = run("create", "--passphrase-file", str(passfile), "--out", str(out))
        assert result.exit_code == 1 and out.read_text() == "precious"

    def test_not_in_a_project(self, tmp_path, monkeypatch, pg):
        monkeypatch.chdir(tmp_path)
        assert run("create").exit_code == 1


@pytest.fixture
def archive(instance, passfile) -> Path:
    run("create", "--passphrase-file", str(passfile))
    return archives(instance)[0]


class TestVerify:
    def test_a_sound_backup(self, archive, pg, passfile):
        result = run("verify", str(archive), "--passphrase-file", str(passfile))
        assert result.exit_code == 0 and "OK" in flat(result)
        assert ("describe", "database.dump") in pg.calls  # the dump was actually read

    def test_a_wrong_passphrase_fails(self, archive, tmp_path):
        wrong = tmp_path / "wrong.txt"
        wrong.write_text("not the right passphrase")
        result = run("verify", str(archive), "--passphrase-file", str(wrong))
        assert result.exit_code == 1 and "wrong passphrase" in flat(result)

    def test_the_key_check_can_be_skipped(self, archive):
        result = run("verify", str(archive), "--no-key-check")
        assert result.exit_code == 0 and "key was not checked" in flat(result)

    def test_a_damaged_backup_is_named_and_fails(self, archive, passfile):
        raw = bytearray(archive.read_bytes())
        with tarfile.open(archive) as tar:
            member = tar.getmember("files/filer/a.txt")
        raw[member.offset_data] ^= 0xFF
        archive.write_bytes(bytes(raw))
        result = run("verify", str(archive), "--passphrase-file", str(passfile))
        assert result.exit_code == 1 and "files/filer/a.txt is damaged" in flat(result)

    def test_something_that_is_not_a_backup(self, tmp_path, instance):
        junk = tmp_path / "junk.tar"
        junk.write_bytes(b"nope")
        result = run("verify", str(junk), "--no-key-check")
        assert result.exit_code == 1 and "not a readable backup" in flat(result)


class TestRestore:
    def into(self, tmp_path) -> Path:
        return tmp_path / "restored"

    def test_it_will_not_pick_the_database_for_you(self, archive, passfile, tmp_path, pg):
        result = run("restore", str(archive), "--into", str(self.into(tmp_path)), "--passphrase-file", str(passfile))
        assert result.exit_code == 1 and "--database-url" in flat(result) and "--skip-database" in flat(result)
        assert not self.into(tmp_path).exists() and not [c for c in pg.calls if c[0] == "restore"]

    def test_into_an_empty_database_it_restores_everything_and_repoints_the_project(self, archive, passfile, tmp_path, pg):
        target = self.into(tmp_path)
        result = run("restore", str(archive), "--into", str(target), "--database-url", NEW_DSN, "--passphrase-file", str(passfile))
        assert result.exit_code == 0, result.output
        assert ("restore", NEW_DSN, False, True, b"PGDMP-fake") in pg.calls  # not replacing, portable
        restored = SettingsManager(target / ".arc")
        assert restored.get("pgdb_dsn", reveal=True) == NEW_DSN  # NOT the original: it must not point at the live one
        assert restored.get("api_token", reveal=True) == "s3cret"
        assert (target / "files" / "a.txt").read_text() == "uploaded"
        assert "restoredb" in flat(result) and "reinstall the code" in flat(result)
        assert "files [filer]: " in flat(result) and str(target / "files") in flat(result)

    def test_a_database_that_already_has_tables_is_refused(self, archive, passfile, tmp_path, pg):
        pg.target_is_empty = False
        result = run("restore", str(archive), "--into", str(self.into(tmp_path)), "--database-url", NEW_DSN, "--passphrase-file", str(passfile))
        assert result.exit_code == 1 and "already has tables" in flat(result)
        assert not self.into(tmp_path).exists() and not [c for c in pg.calls if c[0] == "restore"]

    def test_unless_forced_which_replaces_what_is_there(self, archive, passfile, tmp_path, pg):
        pg.target_is_empty = False
        result = run("restore", str(archive), "--into", str(self.into(tmp_path)), "--database-url", NEW_DSN, "--passphrase-file", str(passfile), "--force")
        assert result.exit_code == 0 and ("restore", NEW_DSN, True, True, b"PGDMP-fake") in pg.calls

    def test_skipping_the_database_warns_that_the_project_still_points_at_the_original(self, archive, passfile, tmp_path, pg):
        target = self.into(tmp_path)
        result = run("restore", str(archive), "--into", str(target), "--skip-database", "--passphrase-file", str(passfile))
        assert result.exit_code == 0 and "still points at the original" in flat(result)
        assert SettingsManager(target / ".arc").get("pgdb_dsn", reveal=True) == DSN
        assert not [c for c in pg.calls if c[0] == "restore"]

    def test_a_wrong_passphrase_writes_nothing_and_touches_no_database(self, archive, tmp_path, pg):
        wrong = tmp_path / "wrong.txt"
        wrong.write_text("not the right passphrase")
        result = run("restore", str(archive), "--into", str(self.into(tmp_path)), "--database-url", NEW_DSN, "--passphrase-file", str(wrong))
        assert result.exit_code == 1 and "wrong passphrase" in flat(result)
        assert not self.into(tmp_path).exists() and not [c for c in pg.calls if c[0] == "restore"]

    def test_a_failed_database_restore_says_the_local_state_is_there_but_the_database_is_not(self, archive, passfile, tmp_path, pg):
        pg.fail_restore = True
        result = run("restore", str(archive), "--into", str(self.into(tmp_path)), "--database-url", NEW_DSN, "--passphrase-file", str(passfile))
        assert result.exit_code == 1 and "already exists" in flat(result) and "database was not" in flat(result)
        assert "Traceback" not in result.output

    def test_it_does_not_write_over_an_existing_project(self, archive, passfile, instance, pg):
        result = run("restore", str(archive), "--into", str(instance), "--skip-database", "--passphrase-file", str(passfile))
        assert result.exit_code == 1 and "already exists" in flat(result)

    def test_the_target_database_must_be_reachable(self, archive, passfile, tmp_path, pg, monkeypatch):
        monkeypatch.setattr(pg, "is_empty", lambda dsn: (_ for _ in ()).throw(OSError("connection refused")))
        result = run("restore", str(archive), "--into", str(self.into(tmp_path)), "--database-url", NEW_DSN, "--passphrase-file", str(passfile))
        assert result.exit_code == 1 and "cannot reach the target database" in flat(result)

    def restore_with_root(self, instance, elsewhere: Path, tmp_path, passfile) -> Path:
        elsewhere.mkdir(parents=True, exist_ok=True)
        (elsewhere / "x.pdf").write_bytes(b"%PDF")
        SettingsManager(instance / ".arc").set("filer_local_root", str(elsewhere))
        run("create", "--passphrase-file", str(passfile), "--out", str(tmp_path / "out.tar"))
        target = tmp_path / "restored"
        result = run("restore", str(tmp_path / "out.tar"), "--into", str(target), "--skip-database", "--passphrase-file", str(passfile))
        assert result.exit_code == 0, result.output
        return target

    def test_an_absolute_files_root_outside_the_project_is_repointed_at_the_restored_copy(self, instance, tmp_path, pg, passfile):
        outside = tmp_path.parent / (tmp_path.name + "-uploads")
        target = self.restore_with_root(instance, outside, tmp_path, passfile)
        assert (target / "restored-files" / "filer" / "x.pdf").read_bytes() == b"%PDF"
        assert SettingsManager(target / ".arc").get("filer_local_root") == str((target / "restored-files" / "filer").resolve())

    def test_and_so_is_an_absolute_root_that_happened_to_be_inside_the_project(self, instance, tmp_path, pg, passfile):
        target = self.restore_with_root(instance, instance / "uploads", tmp_path, passfile)
        assert (target / "uploads" / "x.pdf").read_bytes() == b"%PDF"
        assert SettingsManager(target / ".arc").get("filer_local_root") == str((target / "uploads").resolve())  # not the old path

    def test_a_default_root_stays_unset_so_it_means_the_restored_project_files(self, archive, passfile, tmp_path, pg):
        target = tmp_path / "restored"
        run("restore", str(archive), "--into", str(target), "--skip-database", "--passphrase-file", str(passfile))
        assert SettingsManager(target / ".arc").get("filer_local_root") is None and (target / "files" / "a.txt").exists()
