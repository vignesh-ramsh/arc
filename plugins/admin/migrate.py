"""
plugins.admin.migrate
=====================
Trigger ``arc db migrate`` from the admin panel's **Migrate** button.

Migrate is run as a SUBPROCESS, not in-process. That is deliberate, not a
shortcut: the migration pipeline opens its own AUTOCOMMIT read connection and
issues DDL, and running it inside the live server's event loop / connection
pool would be fragile. A subprocess is exactly what a human typing the CLI does
— isolated, and it cannot corrupt the serving process's state.

The subprocess call is blocking, so it is dispatched to a worker thread to keep
the event loop free.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

MIGRATE_TIMEOUT_SECONDS = 180


def _run_blocking(cmd: list[str], cwd: Path) -> dict:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=MIGRATE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return {
            "ok": False, "returncode": -1, "stdout": "", "command": cmd,
            "stderr": f"migrate command not found: {cmd[0]!r}. "
                      f"Set [plugins.admin] migrate_command in arc.toml.",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False, "returncode": -1, "command": cmd,
            "stdout": exc.stdout or "", "stderr": (exc.stderr or "")
            + f"\nmigrate timed out after {MIGRATE_TIMEOUT_SECONDS}s",
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "command": cmd,
    }


async def run_migrate(*, confirm_destructive: bool, cwd: Path,
                      command: list[str]) -> dict:
    """Run the configured migrate command. ``--confirm-destructive`` is appended
    only when the caller opts in. Returns the captured result."""
    cmd = list(command)
    if confirm_destructive:
        cmd.append("--confirm-destructive")
    return await asyncio.to_thread(_run_blocking, cmd, cwd)


async def run_plan(*, cwd: Path, command: list[str]) -> dict:
    """Run a dry-run plan (``arc db plan``) by swapping the verb. Lets the UI
    preview DDL before applying. Falls back gracefully if the command shape is
    custom (no 'migrate' token to swap)."""
    cmd = list(command)
    swapped = ["plan" if part == "migrate" else part for part in cmd]
    return await asyncio.to_thread(_run_blocking, swapped, cwd)


__all__ = ["run_migrate", "run_plan", "MIGRATE_TIMEOUT_SECONDS"]
