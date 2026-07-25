"""arc.tz — the one setting (arc_server_timezone) every plugin that touches
a date/time is meant to read through server_timezone(), rather than each
hardcoding its own UTC assumption."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from arc.runtime import BootError, boot
from arc.settings import SettingsManager
from arc.tz import DEFAULT_SERVER_TIMEZONE, SERVER_TIMEZONE_KEY, server_timezone


def _set_arc_toml_value(project_root: Path, key: str, value: str) -> None:
    import tomlkit

    toml_path = project_root / ".arc" / "arc.toml"
    doc = tomlkit.parse(toml_path.read_text())
    doc.setdefault("settings", tomlkit.table())[key] = value
    toml_path.write_text(tomlkit.dumps(doc))


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
