"""Shared fixtures for the kernel's own test suite.

Kernel tests never touch Postgres/Redis — a temp .arc/ project directory
(SettingsManager's whole surface is file-based) plus duck-typed fake entry
points (resolver.py's own documented test seam — PluginSpec.entry_point's
docstring: "importlib.metadata.EntryPoint in production; tests inject
duck-typed fakes exposing .name / .value / .load()") is enough to exercise
boot()/Kernel()/SettingsManager for real, without booting a single business
plugin. Real plugin behavior belongs to each plugin's own tests/ (and the
Postgres-backed integration suite), not here.
"""

from __future__ import annotations

import secrets as stdlib_secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import tomlkit

from arc import runtime as arc_runtime


class FakeEntryPoint:
    """Duck-typed stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name: str, register: Callable[[Any], None]):
        self.name = name
        self.value = f"testpkg.{name}:register"
        self._register = register

    def load(self) -> Callable[[Any], None]:
        return self._register


@pytest.fixture(autouse=True)
def _reset_kernel():
    """boot() is idempotent within a process — a second call returns the
    SAME kernel unless force=True — which would silently leak state (and
    declared settings specs) across otherwise-independent tests without
    this."""
    arc_runtime.shutdown()
    yield
    arc_runtime.shutdown()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal, valid ARC project directory: .arc/arc.toml plus a real
    arc.mkey (same 32-byte hex `arc init` generates, arc/arc/cli.py's own
    `init` command) — needed for any test that sets/reveals a secret, not
    just plain settings. No plugins.lock — registry.load_lock() already
    treats a missing lock as an empty plugins table, which is exactly what
    most kernel tests want (no plugins to resolve at all)."""
    arc_dir = tmp_path / ".arc"
    arc_dir.mkdir()
    doc = tomlkit.document()
    doc["project"] = tomlkit.table()
    doc["project"]["name"] = "testproj"
    doc["settings"] = tomlkit.table()
    doc["secrets"] = tomlkit.table()
    doc["secrets"]["provider"] = "local_file"
    doc["secrets"]["declared"] = tomlkit.array()
    (arc_dir / "arc.toml").write_text(tomlkit.dumps(doc))
    (arc_dir / "arc.mkey").write_text(stdlib_secrets.token_hex(32))
    return tmp_path


def write_lock(project_root: Path, specs: list[dict]) -> None:
    """Write .arc/plugins.lock from a list of dicts shaped like
    {"name": ..., "capability": ..., "requires": [...],
    "optional_requires": [...], "enabled": True}. Only `name` is required —
    everything else defaults the same way a fresh `arc build` would."""
    doc = tomlkit.document()
    plugins_table = tomlkit.table()
    for spec in specs:
        entry = tomlkit.table()
        entry["version"] = spec.get("version", "0.1.0")
        entry["capability"] = spec.get("capability", spec["name"])
        entry["requires"] = list(spec.get("requires", []))
        entry["optional_requires"] = list(spec.get("optional_requires", []))
        entry["enabled"] = spec.get("enabled", True)
        plugins_table[spec["name"]] = entry
    doc["plugins"] = plugins_table
    (project_root / ".arc" / "plugins.lock").write_text(tomlkit.dumps(doc))
