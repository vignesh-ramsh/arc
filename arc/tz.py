"""
arc.tz
-----------------
The ONE setting that decides what timezone Postgres timestamps are stored/
displayed in, and what timezone a naive (no explicit UTC offset) date/time
value arriving over HTTP is interpreted in. Declared once, here, at boot —
every plugin that touches a date/time (psqldb's connection setup, relay's
kwarg coercion) reads the SAME value through server_timezone(), so they can
never quietly disagree with each other.

Deliberately not a hardcoded UTC-only story: a deployment whose users/data
all live in one real-world timezone can set arc_server_timezone once and
have every date/time value naturally read/write in THAT zone. Postgres's
own TIMESTAMPTZ handling already does correct UTC<->local conversion once a
session's own `timezone` setting matches (see psqldb's connection-init
hook, PsqlDbProvider.open) — nothing here reinvents that math by hand.

Entirely local: Python's stdlib `zoneinfo` reads timezone definitions from
the operating system's own tzdata — never the network, at any point.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import settings as _settings
from .settings import SettingsError

SERVER_TIMEZONE_KEY = "arc_server_timezone"
DEFAULT_SERVER_TIMEZONE = "UTC"


def declare(kernel: Any) -> None:
    """Called once from arc.runtime.boot(), before any plugin's own
    register() runs — so every plugin, whatever order it loads in, can
    already call server_timezone() from inside its own register()."""
    kernel.settings.declare(
        SERVER_TIMEZONE_KEY,
        type=str,
        default=DEFAULT_SERVER_TIMEZONE,
        doc=(
            "IANA timezone name (e.g. 'UTC', 'Asia/Kolkata', 'America/New_York') "
            "used for every date/time Postgres stores/displays, and for "
            "interpreting an incoming date/time value with no explicit UTC offset."
        ),
    )


def server_timezone() -> ZoneInfo:
    """The configured arc_server_timezone as a real tzinfo object. Raises
    SettingsError (naming the bad value, with a fix command) if it isn't a
    real IANA zone name."""
    name = _settings.get(SERVER_TIMEZONE_KEY) or DEFAULT_SERVER_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SettingsError(
            f"'{SERVER_TIMEZONE_KEY}' is set to {name!r}, which is not a "
            f"recognized IANA timezone name (e.g. 'UTC', 'Asia/Kolkata', "
            f"'America/New_York') — fix it with `arc settings set "
            f"{SERVER_TIMEZONE_KEY} <name>`."
        ) from exc
