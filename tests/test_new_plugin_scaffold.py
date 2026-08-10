"""arc.cli._plugin_template_files() — the pure function `arc new-plugin`
writes every scaffolded file from. Covers the py.typed marker specifically
(every real plugin in this repo already ships one; the scaffold used to
be the one place that didn't) — not the full `new-plugin` command itself,
which has real side effects (git init, `uv sync`, plugins.lock) out of
scope for a kernel-level unit test."""

from __future__ import annotations

from arc.cli import _plugin_template_files


class TestPyTyped:
    def test_scaffold_includes_an_empty_py_typed_marker(self):
        files = _plugin_template_files("widgets")
        assert "widgets/py.typed" in files
        assert files["widgets/py.typed"] == ""

    def test_py_typed_lives_inside_the_package_directory_not_the_plugin_root(self):
        """Must sit next to __init__.py (plugins/<name>/<name>/py.typed),
        not at plugins/<name>/py.typed — hatchling's packages=["<name>"]
        only bundles what's inside the package dir itself."""
        files = _plugin_template_files("widgets")
        assert "widgets/py.typed" in files
        assert "py.typed" not in files  # would mean it landed at the plugin root instead


class TestScaffoldShape:
    def test_every_relative_path_is_relative_not_absolute(self):
        for rel_path in _plugin_template_files("widgets"):
            assert not rel_path.startswith("/")

    def test_scaffold_works_for_a_different_plugin_name(self):
        files = _plugin_template_files("timesheets")
        assert "timesheets/py.typed" in files
        assert files["timesheets/py.typed"] == ""


class TestEventsScaffold:
    """events.py — the arc.events.register_from() counterpart of hooks/
    api/tasks's own example files, but with one real constraint theirs
    don't have: register_from() requires a callable register(kernel), so
    the scaffold's register() must stay real and uncommented even though
    its example subscription is commented out (see _plugin_template_files'
    own docstring)."""

    def test_events_py_lives_at_the_plugin_root_not_inside_the_package(self):
        files = _plugin_template_files("widgets")
        assert "events.py" in files
        assert "widgets/events.py" not in files

    def test_events_py_defines_a_real_uncommented_register_function(self):
        content = _plugin_template_files("widgets")["events.py"]
        assert "\ndef register(kernel)" in content

    def test_init_py_wires_register_from_at_the_plugin_root_path(self):
        content = _plugin_template_files("widgets")["widgets/__init__.py"]
        assert 'arc.events.register_from(Path(__file__).parent.parent / "events.py")' in content
