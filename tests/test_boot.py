"""arc.runtime.boot() — end to end against a temp project directory, with
duck-typed fake entry points standing in for real installed plugins (no
pgdb/redix/real business plugin is ever imported here)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arc.kernel import ExportError
from arc.resolver import ResolutionError
from arc.runtime import BootError, boot, shutdown
from arc.settings import SettingsManager

from .conftest import FakeEntryPoint, write_lock


def _set_arc_toml_value(project_root: Path, key: str, value: str) -> None:
    # Despite the name (kept for a minimal diff against callers), this goes
    # through arc.store.db via SettingsManager directly now, not arc.toml —
    # settings values moved out of arc.toml entirely. A raw arc.toml write
    # would only take effect via the one-time legacy migration on a
    # project's FIRST SettingsManager construction, which silently no-ops
    # on every subsequent boot — not what a test helper should rely on.
    SettingsManager(project_root / ".arc").set(key, value)


class TestBootBasics:
    def test_boot_with_no_plugins_succeeds_with_no_capabilities(self, project: Path):
        kernel = boot(project_root=project, entry_points=())
        assert kernel.capabilities() == {}

    def test_boot_registers_one_plugin(self, project: Path):
        def register(kernel):
            kernel.export("widget", "the-widget-instance")

        write_lock(project, [{"name": "widget"}])
        kernel = boot(project_root=project, entry_points=[FakeEntryPoint("widget", register)])
        assert kernel.get("widget") == "the-widget-instance"

    def test_boot_is_idempotent(self, project: Path):
        kernel1 = boot(project_root=project, entry_points=())
        kernel2 = boot(project_root=project, entry_points=())
        assert kernel1 is kernel2

    def test_boot_force_rebuilds(self, project: Path):
        kernel1 = boot(project_root=project, entry_points=())
        kernel2 = boot(project_root=project, force=True, entry_points=())
        assert kernel1 is not kernel2

    def test_boot_outside_a_project_raises(self, tmp_path: Path):
        with pytest.raises(BootError):
            boot(project_root=tmp_path / "not-a-project")

    def test_shutdown_clears_the_kernel_so_boot_rebuilds(self, project: Path):
        kernel1 = boot(project_root=project, entry_points=())
        shutdown()
        kernel2 = boot(project_root=project, entry_points=())
        assert kernel1 is not kernel2


class TestBootLoadOrder:
    def test_dependent_registers_after_its_hard_requirement(self, project: Path):
        order: list[str] = []

        def register_base(kernel):
            order.append("base")
            kernel.export("base", object())

        def register_dependent(kernel):
            order.append("dependent")
            assert kernel.has("base")  # the whole point of load order
            kernel.export("dependent", object(), requires=["base"])

        write_lock(
            project,
            [
                {"name": "dependent", "requires": ["base"]},
                {"name": "base"},
            ],
        )
        boot(
            project_root=project,
            entry_points=[
                FakeEntryPoint("dependent", register_dependent),
                FakeEntryPoint("base", register_base),
            ],
        )
        assert order == ["base", "dependent"]

    def test_missing_hard_requirement_raises_resolution_error(self, project: Path):
        def register(kernel):
            kernel.export("dependent", object(), requires=["missing"])

        write_lock(project, [{"name": "dependent", "requires": ["missing"]}])
        with pytest.raises(ResolutionError):
            boot(project_root=project, entry_points=[FakeEntryPoint("dependent", register)])


class TestBootFailureModes:
    def test_register_raising_wraps_as_boot_error_and_tears_down_kernel(self, project: Path):
        def register(kernel):
            raise RuntimeError("plugin exploded")

        write_lock(project, [{"name": "broken"}])
        with pytest.raises(BootError, match="plugin exploded"):
            boot(project_root=project, entry_points=[FakeEntryPoint("broken", register)])

        from arc import _state

        assert _state.get_kernel() is None  # torn down, never left half-booted

        # a later boot() (e.g. after fixing the plugin) must start fresh, not
        # be stuck returning torn-down state — re-resolve against the SAME
        # fake entry point, now behaving correctly, to prove that.
        def register_fixed(kernel):
            kernel.export("broken", object())

        kernel = boot(project_root=project, entry_points=[FakeEntryPoint("broken", register_fixed)])
        assert kernel.has("broken")

    def test_register_that_never_exports_raises_export_error(self, project: Path):
        def register(kernel):
            pass  # never calls kernel.export()

        write_lock(project, [{"name": "lazy"}])
        with pytest.raises(ExportError):
            boot(project_root=project, entry_points=[FakeEntryPoint("lazy", register)])

    def test_register_exporting_wrong_capability_name_raises(self, project: Path):
        def register(kernel):
            kernel.export("wrong_name", object())

        write_lock(project, [{"name": "mismatched", "capability": "mismatched"}])
        with pytest.raises(ExportError):
            boot(project_root=project, entry_points=[FakeEntryPoint("mismatched", register)])


class TestBootValidatesTypedSettings:
    def test_bad_typed_setting_value_fails_boot_with_a_clear_message(self, project: Path):
        def register(kernel):
            kernel.settings.declare("widget_pool_size", type=int, default=5, doc="pool size")
            kernel.export("widget", object())

        _set_arc_toml_value(project, "widget_pool_size", "not-a-number")
        write_lock(project, [{"name": "widget"}])
        with pytest.raises(BootError, match="widget_pool_size"):
            boot(project_root=project, entry_points=[FakeEntryPoint("widget", register)])

    def test_valid_typed_setting_value_boots_cleanly(self, project: Path):
        def register(kernel):
            kernel.settings.declare("widget_pool_size", type=int, default=5, doc="pool size")
            kernel.export("widget", object())

        _set_arc_toml_value(project, "widget_pool_size", "42")
        write_lock(project, [{"name": "widget"}])
        kernel = boot(project_root=project, entry_points=[FakeEntryPoint("widget", register)])
        assert kernel.settings.get("widget_pool_size") == 42
