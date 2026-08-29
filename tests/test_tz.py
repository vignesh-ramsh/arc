"""arc.tz — the one setting (arc_server_timezone) every plugin that touches
a date/time is meant to read through server_timezone(), rather than each
hardcoding its own UTC assumption."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from arc.runtime import BootError, boot
from arc.settings import SettingsManager
from arc.tz import DEFAULT_SERVER_TIMEZONE, SERVER_TIMEZONE_KEY, add, ago, server_timezone, utcnow


def _set_arc_toml_value(project_root: Path, key: str, value: str) -> None:
    # Despite the name (kept for a minimal diff against callers), this goes
    # through arc.store.db via SettingsManager directly now, not arc.toml —
    # see test_boot.py's copy of this same helper for why.
    SettingsManager(project_root / ".arc").set(key, value)


class TestServerTimezoneDeclaredAtBoot:
    def test_defaults_to_utc_when_unset(self, project: Path):
        boot(project_root=project, entry_points=())
        assert server_timezone() == ZoneInfo("UTC")

    def test_declared_key_shows_up_in_list_all(self, project: Path):
        kernel = boot(project_root=project, entry_points=())
        data = kernel.settings.list_all()
        assert data[SERVER_TIMEZONE_KEY]["type"] == "str"
        assert data[SERVER_TIMEZONE_KEY]["default"] == DEFAULT_SERVER_TIMEZONE

    def test_a_real_configured_zone_is_honored(self, project: Path):
        _set_arc_toml_value(project, SERVER_TIMEZONE_KEY, "Asia/Kolkata")
        boot(project_root=project, entry_points=())
        assert server_timezone() == ZoneInfo("Asia/Kolkata")

    def test_an_invalid_zone_name_fails_boot_with_a_clear_message(self, project: Path):
        _set_arc_toml_value(project, SERVER_TIMEZONE_KEY, "Not/A_Real_Zone")
        with pytest.raises(BootError, match="arc_server_timezone"):
            boot(project_root=project, entry_points=())

    def test_server_timezone_without_a_boot_still_works_via_settings_manager(self, project: Path):
        """server_timezone() itself only needs arc.settings to be bound to a
        project (any boot(), even with zero plugins) — it has no dependency
        on any particular plugin being installed."""
        mgr = SettingsManager(project / ".arc")
        mgr.declare(SERVER_TIMEZONE_KEY, type=str, default=DEFAULT_SERVER_TIMEZONE)
        mgr.set(SERVER_TIMEZONE_KEY, "America/New_York")
        assert mgr.get(SERVER_TIMEZONE_KEY) == "America/New_York"


class TestUtcDeltaHelpers:
    """utcnow()/add()/ago() need no boot, no settings, no project at all —
    pure datetime math, deliberately independent of server_timezone()
    (see their own module-level comment for why)."""

    def test_utcnow_is_aware_and_utc(self):
        now = utcnow()
        assert now.tzinfo is timezone.utc

    def test_add_defaults_to_now_plus_delta(self):
        before = utcnow()
        result = add(seconds=60)
        after = utcnow()
        assert before + timedelta(seconds=60) <= result <= after + timedelta(seconds=60)

    def test_add_from_an_explicit_base(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert add(days=30, base=base) == datetime(2026, 1, 31, tzinfo=timezone.utc)

    def test_add_combines_every_unit(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = add(weeks=1, days=1, hours=1, minutes=1, seconds=1, milliseconds=500, base=base)
        assert result == base + timedelta(
            weeks=1, days=1, hours=1, minutes=1, seconds=1, milliseconds=500
        )

    def test_ago_defaults_to_now_minus_delta(self):
        before = utcnow()
        result = ago(days=7)
        after = utcnow()
        assert before - timedelta(days=7) <= result <= after - timedelta(days=7)

    def test_ago_from_an_explicit_base(self):
        base = datetime(2026, 1, 31, tzinfo=timezone.utc)
        assert ago(days=30, base=base) == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_ago_is_the_exact_inverse_of_add(self):
        base = utcnow()
        assert ago(hours=5, base=add(hours=5, base=base)) == base
