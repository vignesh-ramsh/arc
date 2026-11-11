"""
arc.permissions
-----------------
Who else on this machine can read a project's `.arc/` — the settings store (which holds every
plain setting, such as the /metrics token and `restart_command`, plus the encrypted secrets) and
the master key that decrypts those secrets.

A new project is private from the first byte: `.arc/` is 0700 and the store and master key are
created 0600 (SQLite gives the `-wal` and `-shm` files the mode of the main file). A project
created before that has `.arc/` 775 and the store 644 — readable by every local user — and is
repaired explicitly with `arc doctor --fix`, never silently while the app runs: a deployment that
deliberately shares `.arc/` with another account through a group must be able to keep doing so.

The same goes for the rest of what a project writes that other users have no business reading: the
database dumps and backups (every password hash, every row), the logs (client addresses, e-mail
addresses, tracebacks) and the filer's uploaded files. Backups are private like `.arc/` (0700/0600).
Logs and uploads are 0750/0640: nothing for "other" users, but a group the operator put there
deliberately (a log shipper in `adm`, say) can still read. `audit_project()` covers all of them, and
`arc doctor --fix` repairs an older project.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
#: Logs and uploaded files: closed to "other" users, readable by the owner's group.
SHARED_DIR_MODE = 0o750
SHARED_FILE_MODE = 0o640

#: The files in `.arc/` that must not be readable by other users. The `-wal` and `-shm` files exist
#: only while a process has the store open.
PRIVATE_FILES = ("arc.mkey", "arc.store.db", "arc.store.db-wal", "arc.store.db-shm")


def create_private_dir(path: Path, mode: int = PRIVATE_DIR_MODE) -> bool:
    """Create `path` (and missing parents) with mode 0700 - or `mode`, for a directory that is
    private from other users but may be shared with a group (SHARED_DIR_MODE). An existing
    directory is left exactly as it is. True if it was created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        # Another process or thread made it between the check above and now - every web worker and
        # queue worker creates `logs/` when it boots, and two uploads can need the same new folder.
        # Whoever won applies the mode, so this one leaves it alone.
        return False
    path.chmod(mode)  # mkdir's mode is masked by the umask; this sets it exactly
    return True


def ensure_private_file(path: Path) -> None:
    """Make `path` a file only its owner can read, BEFORE something writes secrets into it: create
    it empty with mode 0600, or tighten it if it exists. For output that a tool we run (pg_dump)
    opens by name itself and would otherwise create with the default mode."""
    create_private_file(path)
    path.chmod(PRIVATE_FILE_MODE)


def create_private_file(path: Path) -> bool:
    """Create an empty `path` with mode 0600 unless it exists. True if it was created."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def write_private_file(path: Path, text: str) -> None:
    """Write a new file that was never readable by anyone but its owner — not written with the
    default mode and tightened afterwards, which leaves a window. Fails if it exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


@dataclass(frozen=True)
class Finding:
    """A path that other users on this machine can reach but should not."""

    path: Path
    kind: str  # "directory" | "file"
    mode: int  # permission bits now
    wanted: int
    fixable: bool  # this user owns it (or is root), so chmod will work

    @property
    def why(self) -> str:
        return (
            "other users on this machine can list and enter it"
            if self.kind == "directory"
            else "other users on this machine can read it"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "mode": f"{self.mode:03o}",
            "wanted": f"{self.wanted:03o}",
            "fixable": self.fixable,
        }


def _finding(path: Path, kind: str, wanted: int, *, closed_to: int = 0o077) -> Finding | None:
    """`closed_to` is the set of bits that must be clear: group and others (0o077, the default) for
    something private, or only others (0o007) for something a group may be allowed to read."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(info.st_mode)
    if not mode & closed_to:
        return None
    euid = os.geteuid()
    return Finding(path, kind, mode, wanted, fixable=euid == 0 or info.st_uid == euid)


def audit(arc_dir: Path) -> list[Finding]:
    """What in `arc_dir` (a project's `.arc/`) is reachable by group or others. Read-only. The
    directory first, then the private files that exist. Nothing on platforms without POSIX modes."""
    if os.name != "posix":
        return []
    found = [_finding(arc_dir, "directory", PRIVATE_DIR_MODE)]
    found += [_finding(arc_dir / name, "file", PRIVATE_FILE_MODE) for name in PRIVATE_FILES]
    return [f for f in found if f is not None]


def fix(findings: list[Finding]) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    """chmod each finding to its wanted mode. (fixed, [(finding, reason it could not be)])."""
    fixed: list[Finding] = []
    failed: list[tuple[Finding, str]] = []
    for finding in findings:
        try:
            finding.path.chmod(finding.wanted)
        except OSError as exc:
            failed.append((finding, exc.strerror or str(exc)))
        else:
            fixed.append(finding)
    return fixed, failed


def _children(directory: Path, *, directories: bool) -> list[Path]:
    """The entries of `directory` that are (not) directories, sorted; none if it is missing. Symlinks
    are skipped: chmod would follow them to something this audit knows nothing about."""
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    return [
        e for e in entries
        if not e.is_symlink() and e.is_dir() == directories and e.name != ".gitkeep"
    ]


def audit_project(root: Path) -> list[Finding]:
    """`audit()` of `.arc/` plus everything else a project writes that other users must not read:
    `backups/` (private, 0700/0600: database dumps hold every password hash), `logs/` and the filer's
    default `files/` folder (closed to others, 0750/0640). Read-only. Directories before their files.

    Only the locations the framework itself creates, one level of children deep: the files inside
    `files/` need no entry of their own once the folder above them is closed, and a filer root moved
    elsewhere with `filer_local_root` is the operator's to protect."""
    if os.name != "posix":
        return []
    found = audit(root / ".arc")

    backups = root / "backups"
    found.append(_finding(backups, "directory", PRIVATE_DIR_MODE))
    for sub in _children(backups, directories=True):
        found.append(_finding(sub, "directory", PRIVATE_DIR_MODE))
        found += [_finding(f, "file", PRIVATE_FILE_MODE) for f in _children(sub, directories=False)]
    found += [_finding(f, "file", PRIVATE_FILE_MODE) for f in _children(backups, directories=False)]

    logs = root / "logs"
    found.append(_finding(logs, "directory", SHARED_DIR_MODE, closed_to=0o007))
    found += [_finding(f, "file", SHARED_FILE_MODE, closed_to=0o007) for f in _children(logs, directories=False)]

    files = root / "files"
    found.append(_finding(files, "directory", SHARED_DIR_MODE, closed_to=0o007))
    found += [_finding(d, "directory", SHARED_DIR_MODE, closed_to=0o007) for d in _children(files, directories=True)]
    return [f for f in found if f is not None]
