"""
arc.backup
-----------------
A full backup of an ARC project in ONE file, and a restore that proves it works.

`arc pgdb backup` dumps the database only. That is not enough to bring an instance back: without
`.arc/arc.mkey` every secret in the settings store is unrecoverable, and without the filer's files
every upload is gone. A backup is a single tar holding:

    database.dump        pg_dump custom format (when a database is configured)
    state/arc.store.db   the settings store - a consistent snapshot taken with SQLite's backup API,
                         not a copy of a file that may have a write-ahead log beside it
    state/arc.toml       project config
    state/plugins.lock   which plugins, which versions
    config/**            the config/*.toml overlays
    files/<label>/**     the filer's local files (written AFTER the database dump, so every row in
                         the dump has its file; a file newer than the dump is a harmless orphan)
    key.enc              the master key, encrypted with a passphrase (scrypt + AES-256-GCM)
    manifest.json        format version, what is inside, and a SHA-256 for every member

What is deliberately NOT in it: the code (arc/ and plugins/ are in your git repositories), logs and
runtime state, and `.venv`.

The passphrase protects the master key, the one thing that would make the (encrypted) secrets in the
store readable. It does not encrypt the rest: the database dump and the files are business data,
including password hashes. Treat the archive like the database itself - keep it on encrypted storage
or encrypt it at rest (age, gpg). The checksums detect corruption, not tampering.

Restore refuses to guess. It needs an explicit target database (never the one in the archive's own
settings, which would be the live one), only restores into an empty database unless told to replace,
verifies every checksum and decrypts the key BEFORE touching anything, and will not write over an
existing project directory unless forced.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import permissions

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
KEY = "key.enc"
DATABASE = "database.dump"
STORE = "state/arc.store.db"
TOML = "state/arc.toml"
LOCK = "state/plugins.lock"
CONFIG = "config/"
FILES = "files/"
MIN_PASSPHRASE_LENGTH = 12
ARCHIVE_GLOB = "arc-backup-*.tar"

_SCRYPT = {"n": 2**15, "r": 8, "p": 1}  # ~32 MiB and ~0.1 s: costly to guess, instant to use
_SCRYPT_LIMITS = {"n": (2**14, 2**20), "r": (1, 16), "p": (1, 4)}  # what a restore will accept from a file
_KEY_AAD = b"arc-backup-key-v1"
_CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    """The backup or restore cannot proceed, with a message saying why."""


# --------------------------------------------------------------------------- #
# the master key, protected by a passphrase
# --------------------------------------------------------------------------- #
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def seal_key(master_key: str, passphrase: str) -> bytes:
    """`master_key` (the text of .arc/arc.mkey) encrypted so only `passphrase` opens it."""
    salt, nonce = os.urandom(16), os.urandom(12)
    derived = Scrypt(salt=salt, length=32, **_SCRYPT).derive(passphrase.encode("utf-8"))
    sealed = AESGCM(derived).encrypt(nonce, master_key.encode("utf-8"), _KEY_AAD)
    return json.dumps(
        {"v": 1, "kdf": "scrypt", **_SCRYPT, "salt": _b64(salt), "nonce": _b64(nonce), "ct": _b64(sealed)}
    ).encode("ascii")


def open_key(blob: bytes, passphrase: str) -> str:
    """The master key text, or BackupError for a wrong passphrase / a damaged or hostile blob."""
    try:
        doc = json.loads(blob)
        params = {k: int(doc[k]) for k in _SCRYPT}
        salt, nonce, sealed = (base64.b64decode(doc[k]) for k in ("salt", "nonce", "ct"))
        if doc["v"] != 1 or doc["kdf"] != "scrypt":
            raise ValueError("unknown key format")
    except (ValueError, KeyError, TypeError) as exc:
        raise BackupError(f"the master key in this backup is unreadable ({exc})") from exc
    for name, (low, high) in _SCRYPT_LIMITS.items():
        if not low <= params[name] <= high:  # a hostile file must not be able to make us allocate gigabytes
            raise BackupError(f"the master key in this backup asks for scrypt {name}={params[name]}, outside {low}..{high}")
    derived = Scrypt(salt=salt, length=32, **params).derive(passphrase.encode("utf-8"))
    try:
        return AESGCM(derived).decrypt(nonce, sealed, _KEY_AAD).decode("utf-8")
    except InvalidTag:
        raise BackupError("wrong passphrase, or the master key in this backup was altered") from None


def check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise BackupError(
            f"the passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters: it is the only thing "
            f"protecting the master key in a file that will sit on other machines"
        )


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
class _Reader:
    """Reads exactly `size` bytes of a file (padding with zeros / cutting off if it changed while
    being backed up, so the tar stays well-formed), hashing what it hands over."""

    def __init__(self, handle: BinaryIO, size: int) -> None:
        self._handle, self._left, self._hash, self.short = handle, size, hashlib.sha256(), False

    def read(self, n: int = -1) -> bytes:
        n = self._left if n is None or n < 0 else min(n, self._left)
        data = self._handle.read(n)
        if len(data) < n:  # the file shrank under us
            self.short = True
            data += b"\0" * (n - len(data))
        self._left -= len(data)
        self._hash.update(data)
        return data

    def grew(self) -> bool:
        return bool(self._handle.read(1))

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


@dataclass
class FileRoot:
    """A directory of files to include. `relative` is its path inside the project (so a restore puts
    it back there), or None when it lives elsewhere (restored under the project, and the caller told)."""

    label: str
    path: Path
    relative: str | None = None


@dataclass
class _Writer:
    tar: tarfile.TarFile
    members: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add_bytes(self, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(data), 0o600, int(datetime.now(UTC).timestamp())
        self.tar.addfile(info, io.BytesIO(data))
        self.members[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def add_file(self, name: str, path: Path) -> None:
        stat_result = path.stat()
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = stat_result.st_size, stat_result.st_mode & 0o777, int(stat_result.st_mtime)
        with open(path, "rb") as handle:
            reader = _Reader(handle, stat_result.st_size)
            self.tar.addfile(info, reader)
            changed = reader.short or reader.grew()
        if changed:
            self.warnings.append(f"{path} changed while it was being backed up; the copy may be incomplete")
        self.members[name] = {"size": stat_result.st_size, "sha256": reader.hexdigest()}


def _snapshot_store(source: Path, destination: Path) -> None:
    """A consistent copy of the SQLite settings store: the backup API reads a snapshot even while
    another process writes, where copying the file could miss what is still in the -wal file."""
    src = sqlite3.connect(source)  # read-write on purpose: a WAL-mode database needs its -shm file even to be read
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create(
    project_root: Path,
    out: Path,
    *,
    passphrase: str,
    project_name: str,
    arc_version: str,
    plugins: dict[str, str] | None = None,
    dump_database: Callable[[Path], None] | None = None,
    database_name: str | None = None,
    file_roots: Sequence[FileRoot] = (),
) -> dict[str, Any]:
    """Write the backup to `out` (never over an existing file; 0600; atomically - a failure leaves
    no half-written archive) and return its manifest."""
    check_passphrase(passphrase)
    arc_dir = project_root / ".arc"
    mkey_path, store_path = arc_dir / "arc.mkey", arc_dir / "arc.store.db"
    if not mkey_path.exists() or not store_path.exists():
        raise BackupError(f"{arc_dir} has no arc.mkey / arc.store.db - is this an ARC project?")
    if out.exists():
        raise BackupError(f"{out} already exists; backups are never overwritten")
    permissions.create_private_dir(out.parent)  # an archive holds every row; so does the folder's name list
    partial = out.with_name(out.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    summaries: list[dict[str, Any]] = []
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
            writer = _Writer(tar)
            with tempfile.TemporaryDirectory(dir=out.parent, prefix=".arc-backup-") as work_dir:
                work = Path(work_dir)
                if dump_database is not None:  # first: see the module docstring on ordering
                    dump_path = work / "database.dump"
                    dump_database(dump_path)
                    writer.add_file(DATABASE, dump_path)
                    dump_path.unlink()  # a large dump need not sit in two places at once
                snapshot = work / "arc.store.db"
                _snapshot_store(store_path, snapshot)
                writer.add_file(STORE, snapshot)
                for name, source in ((TOML, arc_dir / "arc.toml"), (LOCK, arc_dir / "plugins.lock")):
                    if source.exists():
                        writer.add_file(name, source)
                config_dir = project_root / "config"
                if config_dir.is_dir():
                    for path in sorted(p for p in config_dir.rglob("*") if p.is_file() and not p.is_symlink()):
                        writer.add_file(CONFIG + path.relative_to(config_dir).as_posix(), path)
                writer.add_bytes(KEY, seal_key(mkey_path.read_text().strip(), passphrase))
                for root in file_roots:
                    count = total = 0
                    if root.path.is_dir():
                        for path in sorted(p for p in root.path.rglob("*") if p.is_file() and not p.is_symlink()):
                            writer.add_file(f"{FILES}{root.label}/{path.relative_to(root.path).as_posix()}", path)
                            count, total = count + 1, total + path.stat().st_size
                    summaries.append(
                        {"label": root.label, "relative": root.relative, "files": count, "bytes": total}
                    )
            manifest = {
                "format": FORMAT_VERSION,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "project": project_name,
                "arc_version": arc_version,
                "plugins": plugins or {},
                "database": {"included": dump_database is not None, "name": database_name},
                "files": summaries,
                "members": dict(writer.members),  # a copy: the manifest is added to the writer's dict next
                "warnings": list(writer.warnings),
            }
            writer.add_bytes(MANIFEST, json.dumps(manifest, indent=2).encode("utf-8"))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(partial, out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return manifest


def archive_name(project_name: str, when: datetime | None = None) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", project_name) or "project"
    return f"arc-backup-{safe}-{(when or datetime.now(UTC)).strftime('%Y%m%dT%H%M%SZ')}.tar"


def prune(directory: Path, keep: int, *, protect: Path | None = None) -> list[Path]:
    """Delete all but the newest `keep` backups in `directory` (by name: the timestamp sorts).
    Only files named like ours; never `protect` (the one just written). Returns what was deleted."""
    found = sorted(directory.glob(ARCHIVE_GLOB))
    doomed = [p for p in found[: max(0, len(found) - keep)] if p != protect]
    for path in doomed:
        path.unlink()
    return doomed


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _check_name(name: str) -> None:
    parts = name.split("/")
    if name.startswith("/") or ".." in parts or "" in parts or "\\" in name or "\0" in name:
        raise BackupError(f"the backup contains an unsafe path {name!r}; refusing to read it")


def _open(archive: Path) -> tarfile.TarFile:
    try:
        return tarfile.open(archive, mode="r:")
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(f"{archive} is not a readable backup ({exc})") from exc


def read_manifest(archive: Path) -> dict[str, Any]:
    with _open(archive) as tar:
        return _manifest(tar)


def _manifest(tar: tarfile.TarFile) -> dict[str, Any]:
    try:
        handle = tar.extractfile(MANIFEST)
        manifest = json.loads(handle.read()) if handle else None
    except (KeyError, ValueError, tarfile.TarError) as exc:
        raise BackupError(f"the backup has no readable {MANIFEST} ({exc}) - it is incomplete or not a backup") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_VERSION:
        raise BackupError(
            f"this backup is format {manifest.get('format') if isinstance(manifest, dict) else '?'}; "
            f"this arc reads format {FORMAT_VERSION}"
        )
    return manifest


def _problems(tar: tarfile.TarFile, manifest: dict[str, Any]) -> list[str]:
    """Every member: safe name, regular file, listed in the manifest, size and SHA-256 as recorded;
    and everything the manifest lists is there."""
    expected: dict[str, dict[str, Any]] = manifest.get("members", {})
    problems: list[str] = []
    seen: set[str] = set()
    for member in tar:
        if member.name == MANIFEST:
            continue
        try:
            _check_name(member.name)
        except BackupError as exc:
            problems.append(str(exc))
            continue
        if not member.isreg():
            problems.append(f"{member.name} is not a regular file")
            continue
        seen.add(member.name)
        if member.name not in expected:
            problems.append(f"{member.name} is in the archive but not in its manifest")
            continue
        digest, handle = hashlib.sha256(), tar.extractfile(member)
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
        record = expected[member.name]
        if member.size != record["size"] or digest.hexdigest() != record["sha256"]:
            problems.append(f"{member.name} is damaged (checksum does not match the manifest)")
    problems += [f"{name} is listed in the manifest but missing from the archive" for name in expected if name not in seen]
    return problems


def verify(
    archive: Path,
    passphrase: str | None = None,
    *,
    check_dump: Callable[[Path], object] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """(manifest, problems). Checks every checksum; with a passphrase, that the master key opens and
    looks like a key; the settings store with SQLite's integrity check; and, with `check_dump`, that
    the database dump is a readable pg_dump archive. An empty problem list means the backup is sound."""
    with _open(archive) as tar:
        manifest = _manifest(tar)
        problems = _problems(tar, manifest)
    if problems:
        return manifest, problems
    with _open(archive) as tar, tempfile.TemporaryDirectory(dir=archive.parent, prefix=".arc-verify-") as work_dir:
        work = Path(work_dir)
        if passphrase is not None:
            try:
                key = open_key(tar.extractfile(KEY).read(), passphrase)
                if not re.fullmatch(r"[0-9a-fA-F]{64}", key.strip()):
                    problems.append("the master key opens, but it is not a 64-digit hex key")
            except KeyError:
                problems.append("the backup has no master key")
            except BackupError as exc:
                problems.append(str(exc))
        _extract(tar, STORE, work / "store.db")
        connection = sqlite3.connect(work / "store.db")
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            result = str(exc)
        finally:
            connection.close()
        if result != "ok":
            problems.append(f"the settings store in the backup is damaged: {result}")
        if check_dump is not None and manifest["database"]["included"]:
            _extract(tar, DATABASE, work / "database.dump")
            try:
                check_dump(work / "database.dump")
            except Exception as exc:  # noqa: BLE001 - any failure of the checker is "this dump is not usable"
                problems.append(f"the database dump is not readable: {exc}")
    return manifest, problems


def _extract(tar: tarfile.TarFile, name: str, dest: Path, *, mode: int = 0o600) -> None:
    source = tar.extractfile(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(source, out, _CHUNK)
    os.chmod(dest, mode)


@dataclass
class RestoreResult:
    manifest: dict[str, Any]
    database_restored: bool
    #: label -> where that directory of files now is
    files: dict[str, Path] = field(default_factory=dict)
    #: label -> directory, for files whose original location was outside the project: they were put
    #: under the project and the caller has to point the matching setting at the new place
    relocated: dict[str, Path] = field(default_factory=dict)


def restore(
    archive: Path,
    target: Path,
    *,
    passphrase: str,
    restore_database: Callable[[Path], None] | None = None,
    skip_files: bool = False,
    force: bool = False,
) -> RestoreResult:
    """Rebuild the project's state under `target`. Order matters: everything is verified and the key
    decrypted before the first byte is written, the database is restored last (after the local state
    it depends on exists), and an existing `target/.arc` stops it unless `force`."""
    with _open(archive) as tar:
        manifest = _manifest(tar)
        problems = _problems(tar, manifest)
    if problems:
        raise BackupError("this backup is damaged, nothing was restored:\n  - " + "\n  - ".join(problems))
    arc_dir = target / ".arc"
    if arc_dir.exists() and not force:
        raise BackupError(f"{arc_dir} already exists - restore into an empty directory, or pass --force to write over it")
    result = RestoreResult(manifest=manifest, database_restored=False)
    if KEY not in manifest["members"]:
        raise BackupError("this backup has no master key, so its secrets could not be read; nothing was restored")
    for entry in manifest.get("files", []):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(entry.get("label", ""))):
            raise BackupError(f"the backup has a file set with an unusable name {entry.get('label')!r}")
    with _open(archive) as tar:
        master_key = open_key(tar.extractfile(KEY).read(), passphrase)  # wrong passphrase stops here, before any write
        target.mkdir(parents=True, exist_ok=True)
        arc_dir.mkdir(mode=0o700, exist_ok=True)
        arc_dir.chmod(0o700)
        key_path = arc_dir / "arc.mkey"
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(master_key)
        os.chmod(key_path, 0o600)
        for name, dest in ((STORE, "arc.store.db"), (TOML, "arc.toml"), (LOCK, "plugins.lock")):
            if name in manifest["members"]:
                _extract(tar, name, arc_dir / dest)
        for name in manifest["members"]:
            if name.startswith(CONFIG):
                _extract(tar, name, target / "config" / name[len(CONFIG):], mode=0o640)
        if not skip_files:
            for entry in manifest.get("files", []):
                if entry["relative"] is not None:
                    base = (target / entry["relative"]).resolve()
                    if target.resolve() not in base.parents and base != target.resolve():
                        raise BackupError(f"the backup wants to put files at {entry['relative']!r}, outside the project")
                else:
                    base = target / "restored-files" / entry["label"]
                    result.relocated[entry["label"]] = base
                prefix = f"{FILES}{entry['label']}/"
                for name in manifest["members"]:
                    if name.startswith(prefix):
                        _extract(tar, name, base / name[len(prefix):], mode=0o640)
                result.files[entry["label"]] = base
        if restore_database is not None and manifest["database"]["included"]:
            with tempfile.TemporaryDirectory(dir=arc_dir, prefix="restore-") as work:
                dump_path = Path(work) / "database.dump"
                _extract(tar, DATABASE, dump_path)
                restore_database(dump_path)
            result.database_restored = True
    return result


def ensure_gitignored(project_root: Path, directory: Path) -> bool:
    """A backup holds business data: make sure git cannot pick it up by accident. If the project has
    a .gitignore that does not already cover `directory`, append it. True if it did."""
    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        return False
    try:
        relative = directory.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return False  # outside the project: git cannot see it
    lines = {line.strip() for line in gitignore.read_text().splitlines()}
    if {relative, relative + "/", relative + "/*", "/" + relative, "/" + relative + "/"} & lines:
        return False
    with gitignore.open("a") as handle:
        handle.write(f"{relative}/*\n")
    return True
