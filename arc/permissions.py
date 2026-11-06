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
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

#: The files in `.arc/` that must not be readable by other users. The `-wal` and `-shm` files exist
#: only while a process has the store open.
PRIVATE_FILES = ("arc.mkey", "arc.store.db", "arc.store.db-wal", "arc.store.db-shm")


def create_private_dir(path: Path) -> bool:
    """Create `path` (and missing parents) with mode 0700. An existing directory is left exactly
    as it is. True if it was created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=PRIVATE_DIR_MODE)
    path.chmod(PRIVATE_DIR_MODE)  # mkdir's mode is masked by the umask; this sets it exactly
    return True


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


def _finding(path: Path, kind: str, wanted: int) -> Finding | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(info.st_mode)
    if not mode & 0o077:  # nothing for group or others
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
