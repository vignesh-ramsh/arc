"""
arc.registry
-------------------
Plugin manifest discovery and the `.arc/plugins.lock` file.

Every plugin directory under `plugins/<name>/` carries a `plugin.toml`
manifest:

    [plugin]
    name = "pgdb"
    version = "0.1.0"
    capability = "pgdb"        # namespace it exports as arc.<capability>
    requires = []                # other capability names this plugin needs
    optional_requires = []       # capability names used IF present, never required

    [dependencies]
    asyncpg = ">=0.29"

`plugins.lock` is the kernel's source of truth for "what did `arc build`
resolve, and which plugins are currently enabled". It is distinct from
"physically present in plugins/" — a plugin can be on disk and disabled,
which means arc.boot() will not call its register() function and its
capability namespace is never attached to `arc`.

A `requires`/`optional_requires` entry is a capability name, OPTIONALLY
followed by a PEP 440 version specifier: plain "pgdb" means "any version
of pgdb boots fine" (unversioned — the default, and the only form that
existed before this); "pgdb>=3.0" or "pgdb>=3.0,<4.0" pins a floor (and
optionally a ceiling) against the *other* plugin's own declared `version`.
parse_requirement()/version_satisfies() below are the one place that
syntax is parsed and checked — resolver.py (the actual boot-time
enforcement) and validate_requires() below (an advisory build-time
preview of the same check) both call through here rather than
duplicating the logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


class RegistryError(RuntimeError):
    pass


_REQUIREMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$")


def parse_requirement(raw: str) -> tuple[str, str | None]:
    """Split one `requires`/`optional_requires` entry into (capability_name,
    version_specifier). "pgdb" -> ("pgdb", None) — unversioned, matches any
    installed version, exactly like every requires entry before this.
    "pgdb>=3.0" -> ("pgdb", ">=3.0"). Raises ValueError (not RegistryError —
    this module has no opinion on whether a caller treats that as a hard
    failure or an advisory warning) if `raw` doesn't even start with a
    capability name."""
    match = _REQUIREMENT_RE.match(raw.strip())
    if not match or not match.group(1):
        raise ValueError(
            f"'{raw}' is not a valid requires entry — expected a capability "
            f"name, optionally followed by a PEP 440 version specifier "
            f"(e.g. 'pgdb>=3.0')."
        )
    name, specifier = match.group(1), match.group(2).strip()
    return name, (specifier or None)


#: The single most common typo when hand-writing a specifier: reaching for
#: "=<"/"=>" (the reading order some other ecosystems use) instead of PEP
#: 440's "<="/">=". Caught explicitly below so the error names the actual
#: fix instead of just "invalid" — confirmed necessary by a real user
#: hitting exactly this on the very first plugin.toml written against it.
_BACKWARDS_OPERATOR_HINTS = {"=<": "<=", "=>": ">="}


def version_satisfies(version: str, specifier: str) -> bool:
    """Whether `version` (the OTHER plugin's own declared version) satisfies
    `specifier` (PEP 440, e.g. '>=3.0,<4.0'). Raises ValueError on a
    malformed specifier or an unparseable version rather than letting
    `packaging`'s own exception types leak past this module's boundary."""
    try:
        spec_set = SpecifierSet(specifier)
    except InvalidSpecifier as exc:
        for backwards, correct in _BACKWARDS_OPERATOR_HINTS.items():
            if backwards in specifier:
                raise ValueError(
                    f"'{specifier}' is not a valid PEP 440 version specifier — "
                    f"'{backwards}' isn't a real operator, did you mean "
                    f"'{specifier.replace(backwards, correct)}' ('{correct}' is)?"
                ) from exc
        raise ValueError(
            f"'{specifier}' is not a valid PEP 440 version specifier — valid "
            f"operators are ~= == != <= >= < > (e.g. '>=3.0', '>=3.0,<4.0')."
        ) from exc
    try:
        parsed_version = Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"'{version}' is not a valid PEP 440 version.") from exc
    return spec_set.contains(parsed_version, prereleases=True)


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


def validate_requires(
    manifests: list[PluginManifest], *, universe: list[PluginManifest] | None = None
) -> list[str]:
    """
    Returns a list of human-readable warnings for any hard `requires`,
    among `manifests`, that isn't satisfied — missing capability, invalid
    requires syntax, or a version the provider doesn't satisfy. Does not
    raise itself — whether an unsatisfied requirement is a hard failure
    or just a warning is entirely the CALLER's decision (`arc build`
    treats it as one; `arc install` still only warns); arc.boot()
    (resolver.py) is what actually enforces it at runtime either way.

    `universe` is what `manifests`' requires get resolved against —
    defaults to `manifests` itself (the original, whole-set behavior).
    Pass a WIDER set here when checking a SUBSET (e.g. `arc build -p
    <plugin>`, checking just that one plugin's own requires) so a
    requirement pointing at a plugin outside the subset still resolves
    against its real, currently-installed version instead of a false
    "not among the plugins being built".
    """
    universe = manifests if universe is None else universe
    version_by_capability = {m.capability: m.version for m in universe}
    warnings = []
    for m in manifests:
        for raw in m.requires:
            try:
                req, spec = parse_requirement(raw)
            except ValueError as exc:
                warnings.append(f"Plugin `{m.name}` has an invalid requires entry: {exc}")
                continue
            if req not in version_by_capability:
                warnings.append(
                    f"Plugin `{m.name}` requires capability `{req}`, which is not "
                    f"among the plugins being built.\n"
                    f"Either add {req} or disable {m.name}."
                )
                continue
            if spec is None:
                continue
            try:
                ok = version_satisfies(version_by_capability[req], spec)
            except ValueError as exc:
                warnings.append(f"Plugin `{m.name}` has an invalid requires entry: {exc}")
                continue
            if not ok:
                warnings.append(
                    f"Plugin `{m.name}` requires `{req}{spec}`, but `{req}` is "
                    f"`{version_by_capability[req]}`.\n"
                    f"Either upgrade {req} or disable {m.name}."
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
