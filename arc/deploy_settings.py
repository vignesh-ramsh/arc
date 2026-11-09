"""
arc.deploy_settings
-------------------
The settings that say how this project is run (restarted, which job queues get a worker). `arc restart`,
`arc build`, `arc run` and `arc deploy` read them, but until now nothing declared them: they were invisible on
the admin Settings page until someone had set one, and had no description. Declared once, at boot, so they show
up there with a description wherever they are read.
"""

from __future__ import annotations

from typing import Any

RESTART_COMMAND_KEY = "restart_command"
LINEUP_QUEUES_KEY = "lineup_queues"
GATEWAY_WORKERS_KEY = "gateway_workers"
LINEUP_WORKERS_KEY = "lineup_workers"


def declare(kernel: Any) -> None:
    kernel.settings.declare(
        RESTART_COMMAND_KEY,
        doc="The command that restarts ARC. `arc restart` (and `arc build`) run it exactly as written, for example "
        "`systemctl --user restart arc-desk.target`. `arc deploy` sets it for you. If empty, `arc restart` says "
        "there is nothing to run.",
    )
    kernel.settings.declare(
        LINEUP_QUEUES_KEY,
        doc="Which job queues the worker processes handle, as a comma-separated list such as `default,mail`. If "
        "empty, ARC uses every queue its installed plugins declare. `arc deploy` reads it to make one worker unit "
        "per queue.",
    )
    kernel.settings.declare(
        GATEWAY_WORKERS_KEY,
        doc="How many web workers (server processes that answer requests) ARC starts. If empty, ARC chooses: 2 for a "
        "development setup, or a number worked out from this machine's CPU and memory (at most 8 on its own) for "
        "production. A number you set is used as given, at least 1. Every worker keeps its own database "
        "connections, so more workers need a bigger database connection limit. Apply a change with `arc deploy`.",
    )
    kernel.settings.declare(
        LINEUP_WORKERS_KEY,
        doc="How many background-job workers ARC starts for each job queue (with `arc run` it is the total). If "
        "empty, ARC chooses: 1 for each queue in a development setup, or a number worked out from this machine for "
        "production, shared over the queues. A number you set is used as given, at least 1. Apply a change with "
        "`arc deploy`.",
    )
