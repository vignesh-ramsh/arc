"""
arc.deploy
-----------------
Generates and installs a systemd (--user) unit whose ExecStart is `arc
run` — the single-process orchestrator (arc.cli's own `run` command) that
already supervises Gateway + lineup worker(s) + lineup scheduler as ONE
unit. This means `arc deploy setup` only ever needs to write ONE systemd
unit, not three: `Restart=always` on it covers the whole stack, and
`arc restart` only ever needs to bounce one service.

Deliberately narrow, matching docs/arc-kernel-event-process-notification-
proposal.md §13: the Kernel itself stays supervisor-blind (arc.events,
`arc run`, every other core command know nothing about systemd) — this
module is OPT-IN TOOLING, invoked only by the explicit, user-run
`arc deploy setup` command. Nothing else in ARC depends on it existing.

Safe by default: a unit is always written STOPPED and NOT enabled unless
--enable is passed. This matters specifically for a dev box, where the
server should start only when a developer actually runs `arc run` (or
explicitly starts the unit) — never silently on the next reboot, quietly
consuming DB/Redis connections nobody asked for. --enable is the explicit
"yes, this should survive a reboot and auto-restart on crash" opt-in — the
always-on production posture, meant to sit behind a reverse proxy (nginx
et al.), never the default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

UNIT_TEMPLATE = """[Unit]
Description=ARC server ({project_name}) — gateway + lineup, via `arc run`
After=network.target redis-server.service postgresql.service

[Service]
Type=simple
WorkingDirectory={project_root}
ExecStart={arc_bin} run --host {host} --port {port}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""


class DeployError(RuntimeError):
    pass


def unit_name(project_root: Path, *, name: str | None = None) -> str:
    # Derived from the project directory, not a fixed "arc-server" —
    # avoids a silent collision if this ever runs for a second ARC
    # project on the same box.
    return f"{name or ('arc-' + project_root.name)}.service"


def unit_path(unit: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / unit


def generate_unit_text(*, project_root: Path, arc_bin: str, host: str, port: int) -> str:
    return UNIT_TEMPLATE.format(
        project_name=project_root.name, project_root=project_root, arc_bin=arc_bin, host=host, port=port,
    )


def _systemctl(*args: str) -> None:
    result = subprocess.run(["systemctl", "--user", *args])
    if result.returncode != 0:
        raise DeployError(f"systemctl --user {' '.join(args)} exited with code {result.returncode}.")


def is_enabled(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-enabled", unit], capture_output=True, text=True,
    )
    return result.stdout.strip() == "enabled"


def install(
    *, project_root: Path, arc_bin: str, host: str, port: int, name: str | None = None, enable: bool,
) -> tuple[str, Path, bool]:
    """Writes the unit file and runs daemon-reload unconditionally — safe
    to call repeatedly, always refreshes the content (a changed port, a
    moved venv). `enable=True` additionally enables (survives reboot) and
    (re)starts it now. `enable=False` — the default — NEVER disables or
    stops a unit that's already enabled/running from a previous run: it
    only rewrites the file and reloads, leaving whatever state the
    operator already chose untouched, so re-running `arc deploy setup`
    with no flags can never silently take a production instance down.

    Returns (unit_name, unit_path, already_existed)."""
    unit = unit_name(project_root, name=name)
    path = unit_path(unit)
    already_existed = path.is_file()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_unit_text(project_root=project_root, arc_bin=arc_bin, host=host, port=port))

    _systemctl("daemon-reload")

    if enable:
        _systemctl("enable", unit)
        _systemctl("restart", unit)  # restart, not start — picks up new content even if already running

    return unit, path, already_existed
