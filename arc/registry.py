"""
arc.registry
-------------------
Plugin manifest discovery and the `.arc/plugins.lock` file.

Every plugin directory under `plugins/<name>/` carries a `plugin.toml`
manifest:

    [plugin]
    name = "psqldb"
    version = "0.1.0"
    capability = "psqldb"        # namespace it exports as arc.<capability>
    requires = []                # other capability names this plugin needs
    optional_requires = []       # capability names used IF present, never required

    [dependencies]
    asyncpg = ">=0.29"

`plugins.lock` is the kernel's source of truth for "what did `arc build`
resolve, and which plugins are currently enabled". It is distinct from
"physically present in plugins/" — a plugin can be on disk and disabled,
which means arc.boot() will not call its register() function and its
capability namespace is never attached to `arc`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tomlkit


class RegistryError(RuntimeError):
    pass


@dataclass
class PluginManifest:
    name: str
    version: str
    capability: str
    requires: list[str] = field(default_factory=list)
    optional_requires: list[str] = field(default_factory=list)
    source_dir: Path | None = None


def _read_manifest(plugin_toml: Path) -> PluginManifest:
    doc = tomlkit.parse(plugin_toml.read_text())
    plugin_section = doc.get("plugin")
    if not plugin_section:
        raise RegistryError(f"{plugin_toml} is missing a [plugin] section.")

    name = plugin_section.get("name")
    if not name:
        raise RegistryError(f"{plugin_toml} [plugin] section is missing 'name'.")

    return PluginManifest(
        name=name,
        version=plugin_section.get("version", "0.0.0"),
        capability=plugin_section.get("capability", name),
        requires=list(plugin_section.get("requires", [])),
        optional_requires=list(plugin_section.get("optional_requires", [])),
        source_dir=plugin_toml.parent,
    )


def read_manifest(plugin_toml: Path) -> PluginManifest:
    """Public entry point for reading a single plugin.toml (e.g. right after a clone)."""
    return _read_manifest(plugin_toml)


def discover_plugins(plugins_dir: Path, only: str | None = None) -> list[PluginManifest]:
    """
    Scan plugins_dir/*/plugin.toml. If `only` is given, restrict to that
    single plugin directory (used by `arc build -p <name>`).
    """
    if not plugins_dir.exists():
        raise RegistryError(f"Plugins directory not found: {plugins_dir}")

    manifests: list[PluginManifest] = []
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        if only is not None and entry.name != only:
            continue
        manifest_path = entry / "plugin.toml"
        if not manifest_path.exists():
            continue
        manifests.append(_read_manifest(manifest_path))

    if only is not None and not manifests:
        raise RegistryError(
            f"No plugin named '{only}' found under {plugins_dir} "
            f"(expected {plugins_dir / only / 'plugin.toml'})."
        )
    return manifests


def validate_requires(manifests: list[PluginManifest]) -> list[str]:
    """
    Returns a list of human-readable warnings for any hard `requires` that
    isn't satisfied by the given set of manifests. Does not raise — this is
    advisory at build time; arc.boot() is what enforces it at runtime.
    """
    available = {m.capability for m in manifests}
    warnings = []
    for m in manifests:
        for req in m.requires:
            if req not in available:
                warnings.append(
                    f"Plugin '{m.name}' requires capability '{req}', "
                    f"which is not among the plugins being built."
                )
    return warnings


# ---------------------------------------------------------------------- #
# plugins.lock
# ---------------------------------------------------------------------- #

def load_lock(lock_path: Path) -> tomlkit.TOMLDocument:
    if not lock_path.exists():
        doc = tomlkit.document()
        doc["plugins"] = tomlkit.table()
        return doc
    return tomlkit.parse(lock_path.read_text())


def save_lock(lock_path: Path, doc: tomlkit.TOMLDocument) -> None:
    lock_path.write_text(tomlkit.dumps(doc))


def merge_manifests_into_lock(
    lock_doc: tomlkit.TOMLDocument, manifests: list[PluginManifest]
) -> tomlkit.TOMLDocument:
    """
    Update lock entries for the given manifests. Preserves the existing
    `enabled` flag for plugins already in the lock; defaults new plugins
    to enabled=true. Plugins not in `manifests` are left untouched (this
    lets `-p` scoped builds update one entry without disturbing the rest).
    """
    plugins_table = lock_doc.setdefault("plugins", tomlkit.table())

    for m in manifests:
        existing = plugins_table.get(m.name)
        enabled = existing.get("enabled", True) if existing else True

        entry = tomlkit.table()
        entry["version"] = m.version
        entry["capability"] = m.capability
        entry["requires"] = m.requires
        entry["optional_requires"] = m.optional_requires
        entry["enabled"] = enabled
        plugins_table[m.name] = entry

    return lock_doc


def set_enabled(lock_doc: tomlkit.TOMLDocument, name: str, enabled: bool) -> None:
    plugins_table = lock_doc.get("plugins")
    if not plugins_table or name not in plugins_table:
        available = list(plugins_table.keys()) if plugins_table else []
        raise RegistryError(
            f"Plugin '{name}' is not in plugins.lock. "
            f"Run `arc build` first. Known plugins: {available or 'none'}"
        )
    plugins_table[name]["enabled"] = enabled


def list_plugins(lock_doc: tomlkit.TOMLDocument) -> list[tuple[str, dict]]:
    plugins_table = lock_doc.get("plugins", {})
    return [(name, dict(entry)) for name, entry in plugins_table.items()]
