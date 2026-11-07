"""`arc backup` - one file holding everything needed to bring an instance back (arc/backup.py).

A backup that cannot be restored is worse than none: it is trusted. So these tests are mostly about
what must NOT happen - a wrong passphrase writing anything, a damaged or hostile archive being
half-restored, a restore landing on the live database or over an existing project - and about the
one thing that makes the secrets recoverable at all: the master key, protected by a passphrase.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arc import backup
from arc.settings import SettingsManager

PASSPHRASE = "correct horse battery staple"


@pytest.fixture
def instance(project: Path) -> Path:
    """A project with a settings store holding a plain setting and a secret, config overlays and uploaded files."""
    mgr = SettingsManager(project / ".arc")
    mgr.declare("api_token", secret=True)
    mgr.set("api_token", "s3cret-value", secret=True)
    mgr.set("greeting", "hello")
    (project / ".arc" / "plugins.lock").write_text('[plugins.pgdb]\nversion = "0.1.0"\n')
    (project / "config").mkdir()
    (project / "config" / "prod.toml").write_text("# prod overlay\n")
    (project / "files" / "private").mkdir(parents=True)
    (project / "files" / "private" / "invoice.txt").write_text("invoice 42")
    (project / "files" / "a.bin").write_bytes(os.urandom(5000))
    return project


def fake_dump(content: bytes = b"PGDMP-pretend-dump"):
    def dump(path: Path) -> None:
        path.write_bytes(content)

    return dump


def make(instance: Path, out: Path | None = None, **kw) -> tuple[Path, dict]:
    out = out or instance / "backups" / "full" / "one.tar"
    kw.setdefault("dump_database", fake_dump())
    kw.setdefault("database_name", "shopdb")
    kw.setdefault("file_roots", [backup.FileRoot("filer", instance / "files", "files")])
    manifest = backup.create(
        instance, out, passphrase=PASSPHRASE, project_name="shop", arc_version="9.9", plugins={"pgdb": "0.1.0"}, **kw
    )
    return out, manifest


def names(archive: Path) -> list[str]:
    with tarfile.open(archive) as tar:
        return tar.getnames()


def rewrite(archive: Path, *, drop: str | None = None, add: tuple[str, bytes] | None = None, damage: str | None = None) -> Path:
    """A modified copy of `archive`: without a member, with an extra one, or with one's bytes flipped."""
    out = archive.with_name("modified.tar")
    with tarfile.open(archive) as src, tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as dst:
        for member in src:
            if member.name == drop:
                continue
            data = src.extractfile(member).read() if member.isreg() else None
            if member.name == damage:
                data = bytes([data[0] ^ 0xFF]) + data[1:]
            dst.addfile(member, io.BytesIO(data) if data is not None else None)
        if add:
            info = tarfile.TarInfo(add[0])
            info.size = len(add[1])
            dst.addfile(info, io.BytesIO(add[1]))
    return out


class TestTheKey:
    def test_it_opens_with_the_passphrase_it_was_sealed_with(self):
        assert backup.open_key(backup.seal_key("ab" * 32, PASSPHRASE), PASSPHRASE) == "ab" * 32

    def test_a_wrong_passphrase_is_refused(self):
        with pytest.raises(backup.BackupError, match="wrong passphrase"):
            backup.open_key(backup.seal_key("ab" * 32, PASSPHRASE), "another passphrase entirely")

    def test_a_changed_byte_is_refused_not_decrypted_to_garbage(self):
        doc = json.loads(backup.seal_key("ab" * 32, PASSPHRASE))
        raw = bytearray(__import__("base64").b64decode(doc["ct"]))
        raw[0] ^= 1
        doc["ct"] = __import__("base64").b64encode(bytes(raw)).decode()
        with pytest.raises(backup.BackupError, match="wrong passphrase, or"):
            backup.open_key(json.dumps(doc).encode(), PASSPHRASE)

    def test_every_seal_differs(self):
        assert backup.seal_key("k" * 64, PASSPHRASE) != backup.seal_key("k" * 64, PASSPHRASE)

    def test_the_key_text_is_not_in_the_blob(self):
        assert b"abcdef0123" not in backup.seal_key("abcdef0123" * 6 + "abcd", PASSPHRASE)

    @pytest.mark.parametrize("params", [{"n": 2**30}, {"n": 2}, {"r": 1000}, {"p": 99}])
    def test_a_hostile_blob_cannot_make_us_allocate_gigabytes(self, params):
        doc = {**json.loads(backup.seal_key("k" * 64, PASSPHRASE)), **params}
        with pytest.raises(backup.BackupError, match="outside"):
            backup.open_key(json.dumps(doc).encode(), PASSPHRASE)

    @pytest.mark.parametrize("blob", [b"", b"not json", b"[]", b'{"v": 2}', b'{"v": 1, "kdf": "md5"}'])
    def test_junk_is_an_error_not_a_traceback(self, blob):
        with pytest.raises(backup.BackupError, match="unreadable"):
            backup.open_key(blob, PASSPHRASE)

    @pytest.mark.parametrize("short", ["", "short", "elevenchars"])
    def test_a_short_passphrase_is_refused_when_making_one(self, short):
        with pytest.raises(backup.BackupError, match="at least 12"):
            backup.check_passphrase(short)


class TestCreating:
    def test_what_is_inside(self, instance):
        archive, manifest = make(instance)
        assert sorted(names(archive)) == sorted(
            [
                "database.dump", "state/arc.store.db", "state/arc.toml", "state/plugins.lock",
                "config/prod.toml", "key.enc", "files/filer/a.bin", "files/filer/private/invoice.txt", "manifest.json",
            ]
        )
        assert manifest["format"] == 1 and manifest["project"] == "shop" and manifest["arc_version"] == "9.9"
        assert manifest["database"] == {"included": True, "name": "shopdb"}
        assert manifest["files"] == [{"label": "filer", "relative": "files", "files": 2, "bytes": 5000 + len("invoice 42")}]

    def test_the_database_is_written_before_the_files(self, instance):
        """Files after the dump: every row in the dump has its file; a newer file is a harmless orphan."""
        order = names(make(instance)[0])
        assert order.index("database.dump") < order.index("files/filer/a.bin")

    def test_the_manifest_holds_a_checksum_for_every_member(self, instance):
        import hashlib

        archive, manifest = make(instance)
        with tarfile.open(archive) as tar:
            for name, record in manifest["members"].items():
                data = tar.extractfile(name).read()
                assert record["size"] == len(data) and record["sha256"] == hashlib.sha256(data).hexdigest()
        assert set(manifest["members"]) == set(names(archive)) - {"manifest.json"}

    def test_the_archive_is_private_to_its_owner(self, instance):
        archive, _ = make(instance)
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600

    def test_the_master_key_is_not_in_it_in_the_clear(self, instance):
        archive, _ = make(instance)
        key = (instance / ".arc" / "arc.mkey").read_text().strip()
        assert key.encode() not in archive.read_bytes()

    def test_it_never_overwrites_a_backup(self, instance):
        archive, _ = make(instance)
        before = archive.read_bytes()
        with pytest.raises(backup.BackupError, match="never overwritten"):
            make(instance, archive)
        assert archive.read_bytes() == before

    def test_a_failure_leaves_nothing_behind(self, instance):
        def broken(path):
            raise RuntimeError("pg_dump blew up")

        out = instance / "backups" / "full" / "failed.tar"
        with pytest.raises(RuntimeError, match="blew up"):
            make(instance, out, dump_database=broken)
        assert list(out.parent.iterdir()) == []  # no archive, no .partial, no work directory

    def test_without_a_database_it_says_so(self, instance):
        archive, manifest = make(instance, dump_database=None, database_name=None)
        assert manifest["database"]["included"] is False and "database.dump" not in names(archive)

    def test_without_files_there_are_none(self, instance):
        archive, manifest = make(instance, file_roots=[])
        assert manifest["files"] == [] and not any(n.startswith("files/") for n in names(archive))

    def test_symlinks_in_the_files_are_not_followed(self, instance):
        (instance / "files" / "link").symlink_to("/etc/hostname")
        assert not any("link" in n for n in names(make(instance)[0]))

    def test_no_project_no_backup(self, tmp_path):
        with pytest.raises(backup.BackupError, match="not an ARC project|is this an ARC project"):
            backup.create(tmp_path, tmp_path / "x.tar", passphrase=PASSPHRASE, project_name="p", arc_version="1")

    def test_the_settings_store_is_a_consistent_snapshot_even_with_data_still_in_the_wal(self, instance):
        """A WAL-mode store keeps recent writes in arc.store.db-wal: copying the file would miss them."""
        mgr = SettingsManager(instance / ".arc")  # keeps the store open, so nothing is checkpointed
        mgr.set("written_just_now", "present")
        assert (instance / ".arc" / "arc.store.db-wal").stat().st_size > 0
        archive, _ = make(instance)
        with tarfile.open(archive) as tar:
            copy = instance / "snapshot.db"
            copy.write_bytes(tar.extractfile("state/arc.store.db").read())
        rows = dict(sqlite3.connect(copy).execute("SELECT key, value FROM setting WHERE is_secret = 0").fetchall())
        assert rows["written_just_now"] == "present" and rows["greeting"] == "hello"

    def test_a_file_that_changes_while_it_is_copied_keeps_the_archive_whole_and_is_reported(self):
        short = backup._Reader(io.BytesIO(b"abc"), 10)  # promised 10 bytes, has 3
        assert short.read(100) == b"abc" + b"\0" * 7 and short.short is True
        grown = backup._Reader(io.BytesIO(b"0123456789-and-more"), 10)
        assert grown.read(100) == b"0123456789" and grown.grew() is True

    def test_archive_names_are_safe_and_sort_by_time(self):
        a = backup.archive_name("my shop/../x", datetime(2026, 11, 7, 1, 2, 3, tzinfo=timezone.utc))
        b = backup.archive_name("my shop/../x", datetime(2026, 12, 1, tzinfo=timezone.utc))
        assert a == "arc-backup-my_shop_.._x-20261107T010203Z.tar" and a < b
        assert "/" not in a


class TestPruning:
    def test_keeps_the_newest_and_only_touches_its_own_files(self, tmp_path):
        for day in (1, 2, 3, 4):
            (tmp_path / f"arc-backup-shop-2026110{day}T000000Z.tar").write_text("x")
        (tmp_path / "notes.txt").write_text("mine")
        (tmp_path / "arc-backup-shop-latest.tar.partial").write_text("x")
        gone = backup.prune(tmp_path, 2)
        assert sorted(p.name for p in gone) == ["arc-backup-shop-20261101T000000Z.tar", "arc-backup-shop-20261102T000000Z.tar"]
        assert (tmp_path / "notes.txt").exists() and (tmp_path / "arc-backup-shop-latest.tar.partial").exists()

    def test_never_deletes_the_one_just_written(self, tmp_path):
        old = tmp_path / "arc-backup-a-20260101T000000Z.tar"
        new = tmp_path / "arc-backup-a-20260102T000000Z.tar"
        old.write_text("x"), new.write_text("x")
        backup.prune(tmp_path, 1, protect=old)
        assert old.exists() and new.exists()


class TestGitignore:
    def test_a_backup_directory_inside_the_project_is_added(self, tmp_path):
        (tmp_path / ".gitignore").write_text("logs/*\n")
        assert backup.ensure_gitignored(tmp_path, tmp_path / "backups" / "full") is True
        assert "backups/full/*" in (tmp_path / ".gitignore").read_text().splitlines()

    def test_not_twice(self, tmp_path):
        (tmp_path / ".gitignore").write_text("backups/full/*\n")
        assert backup.ensure_gitignored(tmp_path, tmp_path / "backups" / "full") is False
        assert (tmp_path / ".gitignore").read_text() == "backups/full/*\n"

    def test_nothing_to_do_without_a_gitignore_or_outside_the_project(self, tmp_path):
        assert backup.ensure_gitignored(tmp_path, tmp_path / "b") is False
        (tmp_path / ".gitignore").write_text("x\n")
        assert backup.ensure_gitignored(tmp_path, Path("/var/backups")) is False


class TestVerifying:
    def test_a_sound_backup_has_no_problems(self, instance):
        archive, _ = make(instance)
        manifest, problems = backup.verify(archive, PASSPHRASE, check_dump=lambda path: None)
        assert problems == [] and manifest["project"] == "shop"

    def test_a_damaged_member_is_named(self, instance):
        damaged = rewrite(make(instance)[0], damage="files/filer/a.bin")
        _, problems = backup.verify(damaged)
        assert problems == ["files/filer/a.bin is damaged (checksum does not match the manifest)"]

    def test_a_missing_member_is_named(self, instance):
        _, problems = backup.verify(rewrite(make(instance)[0], drop="database.dump"))
        assert problems == ["database.dump is listed in the manifest but missing from the archive"]

    def test_a_member_the_manifest_does_not_know_is_reported(self, instance):
        _, problems = backup.verify(rewrite(make(instance)[0], add=("extra.bin", b"smuggled")))
        assert problems == ["extra.bin is in the archive but not in its manifest"]

    def test_a_wrong_passphrase_is_reported(self, instance):
        _, problems = backup.verify(make(instance)[0], "not the right passphrase")
        assert len(problems) == 1 and "wrong passphrase" in problems[0]

    def test_the_key_is_only_checked_when_a_passphrase_is_given(self, instance):
        assert backup.verify(make(instance)[0])[1] == []

    def test_an_unreadable_dump_is_reported(self, instance):
        def reject(path):
            raise RuntimeError("not a pg_dump archive")

        _, problems = backup.verify(make(instance)[0], PASSPHRASE, check_dump=reject)
        assert problems == ["the database dump is not readable: not a pg_dump archive"]

    def test_the_dump_is_not_checked_when_there_is_none(self, instance):
        archive, _ = make(instance, dump_database=None)
        assert backup.verify(archive, PASSPHRASE, check_dump=lambda p: 1 / 0)[1] == []

    def test_a_damaged_settings_store_is_reported(self, instance):
        # a store that is not a database at all, with a manifest that agrees with its bytes
        archive, manifest = make(instance)
        bad = b"this is not an sqlite file" * 100
        out = archive.with_name("badstore.tar")
        import hashlib

        manifest["members"]["state/arc.store.db"] = {"size": len(bad), "sha256": hashlib.sha256(bad).hexdigest()}
        with tarfile.open(archive) as src, tarfile.open(out, "w") as dst:
            for member in src:
                data = src.extractfile(member).read()
                if member.name == "state/arc.store.db":
                    data, member.size = bad, len(bad)
                if member.name == "manifest.json":
                    data = json.dumps(manifest).encode()
                    member.size = len(data)
                dst.addfile(member, io.BytesIO(data))
        _, problems = backup.verify(out)
        assert len(problems) == 1 and "settings store in the backup is damaged" in problems[0]

    def test_not_a_backup_at_all(self, tmp_path):
        junk = tmp_path / "junk.tar"
        junk.write_bytes(b"definitely not a tar file")
        with pytest.raises(backup.BackupError, match="not a readable backup"):
            backup.verify(junk)

    def test_a_tar_that_is_not_ours_has_no_manifest(self, tmp_path):
        other = tmp_path / "other.tar"
        with tarfile.open(other, "w") as tar:
            info = tarfile.TarInfo("readme.txt")
            info.size = 2
            tar.addfile(info, io.BytesIO(b"hi"))
        with pytest.raises(backup.BackupError, match="manifest.json"):
            backup.verify(other)

    def test_a_backup_from_a_newer_format_is_refused_not_misread(self, instance):
        archive, manifest = make(instance)
        out = archive.with_name("future.tar")
        manifest["format"] = 99
        with tarfile.open(archive) as src, tarfile.open(out, "w") as dst:
            for member in src:
                data = src.extractfile(member).read()
                if member.name == "manifest.json":
                    data = json.dumps(manifest).encode()
                    member.size = len(data)
                dst.addfile(member, io.BytesIO(data))
        with pytest.raises(backup.BackupError, match="format 99"):
            backup.verify(out)

    @pytest.mark.parametrize("evil", ["../escape.txt", "/etc/cron.d/x", "files/../../x", "a//b"])
    def test_a_path_that_escapes_is_refused(self, instance, evil):
        _, problems = backup.verify(rewrite(make(instance)[0], add=(evil, b"x")))
        assert any("unsafe path" in p for p in problems)

    def test_a_link_is_not_a_file(self, instance):
        archive, _ = make(instance)
        out = archive.with_name("link.tar")
        with tarfile.open(archive) as src, tarfile.open(out, "w") as dst:
            for member in src:
                dst.addfile(member, io.BytesIO(src.extractfile(member).read()))
            link = tarfile.TarInfo("config/passwd")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            dst.addfile(link)
        _, problems = backup.verify(out)
        assert any("config/passwd is not a regular file" in p for p in problems)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestRestoring:
    def test_everything_comes_back(self, instance, tmp_path):
        original_key = (instance / ".arc" / "arc.mkey").read_text()
        archive, _ = make(instance)
        target = tmp_path / "restored"
        seen = {}

        def restore_database(dump: Path) -> None:
            seen["dump"] = dump.read_bytes()
            seen["state_existed"] = (target / ".arc" / "arc.store.db").exists()

        result = backup.restore(archive, target, passphrase=PASSPHRASE, restore_database=restore_database)

        assert result.database_restored and seen == {"dump": b"PGDMP-pretend-dump", "state_existed": True}
        assert (target / ".arc" / "arc.mkey").read_text() == original_key
        restored = SettingsManager(target / ".arc")
        assert restored.get("greeting") == "hello"
        assert restored.get("api_token", reveal=True) == "s3cret-value"  # decrypts: the key and the store belong together
        assert (target / "config" / "prod.toml").read_text() == "# prod overlay\n"
        assert (target / "files" / "private" / "invoice.txt").read_text() == "invoice 42"
        assert (target / "files" / "a.bin").read_bytes() == (instance / "files" / "a.bin").read_bytes()
        assert (target / ".arc" / "plugins.lock").read_text() == (instance / ".arc" / "plugins.lock").read_text()

    def test_what_it_writes_is_private(self, instance, tmp_path):
        target = tmp_path / "restored"
        backup.restore(make(instance)[0], target, passphrase=PASSPHRASE)
        assert mode(target / ".arc") == 0o700
        assert mode(target / ".arc" / "arc.mkey") == 0o600 and mode(target / ".arc" / "arc.store.db") == 0o600

    def test_no_temporary_files_are_left_in_the_project(self, instance, tmp_path):
        target = tmp_path / "restored"
        backup.restore(make(instance)[0], target, passphrase=PASSPHRASE, restore_database=lambda p: None)
        assert not [p for p in (target / ".arc").iterdir() if p.name.startswith("restore-")]

    def test_a_wrong_passphrase_writes_nothing(self, instance, tmp_path):
        target = tmp_path / "restored"
        with pytest.raises(backup.BackupError, match="wrong passphrase"):
            backup.restore(make(instance)[0], target, passphrase="not the passphrase!", restore_database=lambda p: pytest.fail("touched the database"))
        assert not target.exists()

    def test_a_damaged_backup_writes_nothing_and_says_what_is_wrong(self, instance, tmp_path):
        target = tmp_path / "restored"
        damaged = rewrite(make(instance)[0], damage="files/filer/a.bin")
        with pytest.raises(backup.BackupError, match="a.bin is damaged"):
            backup.restore(damaged, target, passphrase=PASSPHRASE, restore_database=lambda p: pytest.fail("touched the database"))
        assert not target.exists()

    def test_it_does_not_write_over_an_existing_project(self, instance, tmp_path):
        archive, _ = make(instance)
        with pytest.raises(backup.BackupError, match="already exists"):
            backup.restore(archive, instance, passphrase=PASSPHRASE)
        assert (instance / "files" / "private" / "invoice.txt").read_text() == "invoice 42"

    def test_unless_forced(self, instance, tmp_path):
        archive, _ = make(instance)
        (instance / "config" / "prod.toml").write_text("# changed since\n")
        backup.restore(archive, instance, passphrase=PASSPHRASE, force=True)
        assert (instance / "config" / "prod.toml").read_text() == "# prod overlay\n"

    def test_the_database_is_not_restored_unless_asked(self, instance, tmp_path):
        result = backup.restore(make(instance)[0], tmp_path / "r", passphrase=PASSPHRASE)
        assert result.database_restored is False

    def test_a_database_that_fails_to_restore_is_an_error_not_a_success(self, instance, tmp_path):
        def fail(dump):
            raise RuntimeError("pg_restore: relation already exists")

        with pytest.raises(RuntimeError, match="already exists"):
            backup.restore(make(instance)[0], tmp_path / "r", passphrase=PASSPHRASE, restore_database=fail)

    def test_skipping_the_files(self, instance, tmp_path):
        target = tmp_path / "r"
        result = backup.restore(make(instance)[0], target, passphrase=PASSPHRASE, skip_files=True)
        assert not (target / "files").exists() and result.files == {}

    def test_a_backup_without_a_database_restores_without_calling_the_callback(self, instance, tmp_path):
        archive, _ = make(instance, dump_database=None)
        result = backup.restore(archive, tmp_path / "r", passphrase=PASSPHRASE, restore_database=lambda p: pytest.fail("no dump to restore"))
        assert result.database_restored is False

    def test_files_that_lived_outside_the_project_are_put_under_it_and_reported(self, instance, tmp_path):
        elsewhere = tmp_path / "srv" / "uploads"
        elsewhere.mkdir(parents=True)
        (elsewhere / "report.pdf").write_bytes(b"%PDF")
        archive, _ = make(instance, file_roots=[backup.FileRoot("filer", elsewhere, None)])
        target = tmp_path / "r"
        result = backup.restore(archive, target, passphrase=PASSPHRASE)
        assert result.relocated == {"filer": target / "restored-files" / "filer"}
        assert (target / "restored-files" / "filer" / "report.pdf").read_bytes() == b"%PDF"

    def edited_manifest(self, instance, **changes) -> Path:
        archive, manifest = make(instance)
        manifest["files"][0].update(changes)
        out = archive.with_name("edited.tar")
        with tarfile.open(archive) as src, tarfile.open(out, "w") as dst:
            for member in src:
                data = src.extractfile(member).read()
                if member.name == "manifest.json":
                    data = json.dumps(manifest).encode()
                    member.size = len(data)
                dst.addfile(member, io.BytesIO(data))
        return out

    @pytest.mark.parametrize("relative", ["../outside", "/etc", "files/../../outside"])
    def test_a_manifest_cannot_send_files_outside_the_project(self, instance, tmp_path, relative):
        with pytest.raises(backup.BackupError, match="outside the project"):
            backup.restore(self.edited_manifest(instance, relative=relative), tmp_path / "r", passphrase=PASSPHRASE)
        assert not (tmp_path / "outside").exists()

    @pytest.mark.parametrize("label", ["../../etc", "a/b", "", "x y"])
    def test_a_manifest_cannot_name_a_file_set_with_a_path(self, instance, tmp_path, label):
        with pytest.raises(backup.BackupError, match="unusable name"):
            backup.restore(self.edited_manifest(instance, label=label), tmp_path / "r", passphrase=PASSPHRASE)

    def test_a_backup_without_a_key_is_refused_before_anything_is_written(self, instance, tmp_path):
        archive, manifest = make(instance)
        manifest["members"].pop("key.enc")
        out = archive.with_name("nokey.tar")
        with tarfile.open(archive) as src, tarfile.open(out, "w") as dst:
            for member in src:
                if member.name == "key.enc":
                    continue
                data = src.extractfile(member).read()
                if member.name == "manifest.json":
                    data = json.dumps(manifest).encode()
                    member.size = len(data)
                dst.addfile(member, io.BytesIO(data))
        with pytest.raises(backup.BackupError, match="no master key"):
            backup.restore(out, tmp_path / "r", passphrase=PASSPHRASE)
        assert not (tmp_path / "r").exists()
