"""
arc.kernel.installer
===================
Implements ``arc install`` and friends.

Flow for ``arc install <git-url> --branch <b>``:
    1. git clone --depth 1 --branch <b> <url>  into a temp dir
    2. read plugin.toml  → the plugin's declared name + graph metadata
    3. move temp dir → plugins/<manifest.name>   (folder name from manifest,
       NOT from the repo URL)
    4. pip install the plugin's [python].dependencies
    5. upsert an entry into arc.lock (source / branch / commit pinned)

arc.lock is per-machine (gitignored), so install records the source so the
plugin can be re-fetched (`arc install --all`) or updated (`arc update`).

Plugins are imported as ``plugins.<name>.<entrypoint>`` — the project root is
already on sys.path (loader.inject_project_root), and ``plugins/`` resolves as
a namespace package, so no __init__.py is required at the plugins/ root.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from arc.kernel.state import LocalState

from arc.kernel.loader import (
    LockEntry,
    LockFile,
    PluginLoader,
    find_lock_file,
)
from arc.kernel.logger import get_logger

log = get_logger(__name__)


class InstallerError(Exception):
    """Raised for any install/update failure (clone, manifest, pip, lock)."""


# ── plugin.toml manifest ──────────────────────────────────────────────────────

@dataclass
class PluginManifest:
    name:         str
    entrypoint:   str                      # relative, e.g. "plugin:DatabasePlugin"
    version:      str = "1.0.0"
    provides:     list[str] = field(default_factory=list)
    requires:     list[str] = field(default_factory=list)
    load_order:   int = 100
    critical:     bool = False
    dependencies: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "PluginManifest":
        if not path.is_file():
            raise InstallerError(
                f"plugin.toml not found at {path}. Every installable Arc plugin "
                f"must ship a plugin.toml at its repository root."
            )
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise InstallerError(f"plugin.toml is not valid TOML: {exc}") from exc

        name = raw.get("name")
        entrypoint = raw.get("entrypoint")
        if not name or not entrypoint:
            raise InstallerError(
                "plugin.toml must define at least 'name' and 'entrypoint'."
            )
        if ":" not in entrypoint:
            raise InstallerError(
                f"entrypoint '{entrypoint}' must be 'module:ClassName' "
                f"(relative to the plugin directory)."
            )
        python = raw.get("python", {}) or {}
        return cls(
            name=str(name),
            entrypoint=str(entrypoint),
            version=str(raw.get("version", "1.0.0")),
            provides=list(raw.get("provides", [])),
            requires=list(raw.get("requires", [])),
            load_order=int(raw.get("load_order", 100)),
            critical=bool(raw.get("critical", False)),
            dependencies=list(python.get("dependencies", [])),
        )


# ── git / pip shells ──────────────────────────────────────────────────────────

def _run(cmd: list[str], *, cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError as exc:
        raise InstallerError(f"'{cmd[0]}' not found. Is it installed and on PATH?") from exc
    except subprocess.CalledProcessError as exc:
        raise InstallerError(
            f"command failed: {' '.join(cmd)}\n{exc.stderr.strip()}"
        ) from exc
    return proc.stdout.strip()


def _git_clone(url: str, branch: str, dest: Path) -> None:
    log.info("arc.install.clone", url=url, branch=branch)
    _run(["git", "clone", "--depth", "1", "--branch", branch, url, str(dest)])


def _git_commit(repo: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], cwd=repo)


def _pip_install(deps: list[str]) -> None:
    if not deps:
        return
    log.info("arc.install.pip", deps=deps)
    _run([sys.executable, "-m", "pip", "install", *deps])


# ── lock helpers ──────────────────────────────────────────────────────────────

def _project_root() -> Path:
    return find_lock_file().parent


def _read_lock(project_root: Path) -> LockFile:
    path = project_root / "arc.lock"
    if not path.exists():
        return LockFile()
    import json
    try:
        return LockFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return LockFile()


def _build_entry(
    manifest: PluginManifest, url: str, branch: str, commit: str
) -> LockEntry:
    full_entrypoint = f"plugins.{manifest.name}.{manifest.entrypoint}"
    return LockEntry(
        name=manifest.name,
        version=manifest.version,
        entrypoint=full_entrypoint,
        provides=manifest.provides,
        requires=manifest.requires,
        load_order=manifest.load_order,
        critical=manifest.critical,
        source=url,
        branch=branch,
        commit=commit,
    )


def _upsert_entry(project_root: Path, entry: LockEntry) -> None:
    lock = _read_lock(project_root)
    lock.plugins = [e for e in lock.plugins if e.name != entry.name]
    lock.plugins.append(entry)
    PluginLoader.write_lock(project_root / "arc.lock", lock)


# ── public API ────────────────────────────────────────────────────────────────

def install_from_git(
    url: str,
    branch: str = "main",
    *,
    project_root: Path | None = None,
    force: bool = False,
) -> LockEntry:
    """Clone, install deps, and register a plugin. Returns the lock entry."""
    root = project_root or _project_root()
    plugins_root = root / "plugins"
    plugins_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "clone"
        _git_clone(url, branch, clone_dir)
        manifest = PluginManifest.from_file(clone_dir / "plugin.toml")
        commit   = _git_commit(clone_dir)

        target = plugins_root / manifest.name      # ← name from manifest
        if target.exists():
            if not force:
                raise InstallerError(
                    f"plugins/{manifest.name} already exists. "
                    f"Use --force to replace it, or `arc update {manifest.name}`."
                )
            shutil.rmtree(target)
        shutil.move(str(clone_dir), str(target))

    _pip_install(manifest.dependencies)
    entry = _build_entry(manifest, url, branch, commit)
    _upsert_entry(root, entry)
    log.info("arc.install.done", plugin=manifest.name, commit=commit[:8])
    return entry


def reinstall_all(project_root: Path | None = None) -> list[str]:
    """Re-fetch every plugin recorded in the local arc.lock (rehydrate
    plugins/ after it was cleaned). Reads source/branch from the lock."""
    root = project_root or _project_root()
    lock = _read_lock(root)
    done: list[str] = []
    for entry in lock.plugins:
        if not entry.source:
            log.warning("arc.install.no_source", plugin=entry.name)
            continue
        install_from_git(entry.source, entry.branch or "main",
                          project_root=root, force=True)
        done.append(entry.name)
    return done


def update(name: str, branch: str | None = None,
           project_root: Path | None = None) -> LockEntry:
    """Pull the latest commit for an installed plugin and re-pin the lock."""
    root = project_root or _project_root()
    lock = _read_lock(root)
    entry = next((e for e in lock.plugins if e.name == name), None)
    if entry is None or not entry.source:
        raise InstallerError(f"Plugin '{name}' is not installed from a git source.")
    return install_from_git(entry.source, branch or entry.branch or "main",
                            project_root=root, force=True)


# ── graph-safety for enable/disable ───────────────────────────────────────────

def disable_blockers(
    target: str,
    project_root: Path | None = None,
    already_disabled: set[str] | None = None,
) -> list[tuple[str, str]]:
    """
    Return [(plugin, capability), ...] that would break if ``target`` were
    disabled — i.e. still-active plugins that require a capability ONLY
    ``target`` provides. Empty list means the disable is graph-safe.
    """
    root = project_root or _project_root()
    already_disabled = already_disabled or set()
    lock = _read_lock(root)
    target_entry = next((e for e in lock.plugins if e.name == target), None)
    if target_entry is None:
        return []

    active = [
        e for e in lock.plugins
        if e.name != target and e.name not in already_disabled
    ]
    provided_by_others: set[str] = set()
    for e in active:
        provided_by_others.update(e.provides)

    blockers: list[tuple[str, str]] = []
    for e in active:
        for cap in e.requires:
            if cap in target_entry.provides and cap not in provided_by_others:
                blockers.append((e.name, cap))
    return blockers


def unsatisfied_after_enable(
    target: str,
    project_root: Path | None = None,
    already_disabled: set[str] | None = None,
) -> list[str]:
    """
    Return capabilities the freshly-enabled ``target`` requires but which no
    currently-enabled plugin provides. Non-empty means it will not start until
    those providers are also enabled/installed (a warning, not a hard refusal).
    """
    root = project_root or _project_root()
    already_disabled = (already_disabled or set()) - {target}
    lock = _read_lock(root)
    target_entry = next((e for e in lock.plugins if e.name == target), None)
    if target_entry is None:
        return []

    enabled = [e for e in lock.plugins if e.name not in already_disabled]
    provided: set[str] = set()
    for e in enabled:
        provided.update(e.provides)
    return [cap for cap in target_entry.requires if cap not in provided]

@dataclass
class BuildResult:
    """Outcome of a build_project() run, for the CLI to render."""
    plugins: list[str]                       # plugin names in the lock, load order
    installed_deps: list[str]                # deps pip was asked to install (deduped)
    skipped_dirs: list[str]                  # plugins/<dir> folders with no plugin.toml
    disabled: list[str]                      # locally-disabled plugins still in the lock
    name_mismatches: list[tuple[str, str]]   # (folder_name, manifest_name) when they differ
 
 
def _normalize_entrypoint(dir_name: str, entrypoint: str) -> str:
    """Return the import path the loader needs: ``plugins.<dir>.<module>:<Cls>``.
 
    A manifest entrypoint may be:
      • already absolute  — ``plugins.redix.plugin:RedixPlugin``  → used verbatim
      • relative to the plugin dir — ``plugin:RedixPlugin``        → prefixed
    The module path is keyed off the FOLDER name (the real import root), not the
    manifest's ``name`` field, so a name/folder drift can't produce a bad path.
    """
    module, sep, cls = entrypoint.partition(":")
    if not sep:
        raise InstallerError(
            f"plugins/{dir_name}: entrypoint {entrypoint!r} must be "
            f"'module:ClassName'."
        )
    if module.startswith("plugins."):
        return entrypoint
    return f"plugins.{dir_name}.{module}:{cls}"
 
 
def _discover_local_manifests(plugins_root: Path):
    """Scan plugins/<dir>/plugin.toml.
 
    Returns ``(found, skipped)`` where ``found`` is a list of
    ``(folder_name, PluginManifest)`` and ``skipped`` is the names of folders
    that have no plugin.toml. Hidden / dunder folders (``.git``, ``__pycache__``)
    are ignored silently.
    """
    found: list[tuple[str, "PluginManifest"]] = []
    skipped: list[str] = []
    if not plugins_root.is_dir():
        return found, skipped
    for child in sorted(plugins_root.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        manifest_path = child / "plugin.toml"
        if not manifest_path.is_file():
            skipped.append(child.name)
            continue
        found.append((child.name, PluginManifest.from_file(manifest_path)))
    return found, skipped
 
 
def build_project(project_root: Path | None = None, *,
                  install_deps: bool = True) -> BuildResult:
    """Regenerate arc.lock from plugins/ and install every plugin's deps.
 
    The lock is rebuilt from scratch from whatever plugin folders are present
    (each must carry a plugin.toml). Prior git-source metadata and embedded
    per-plugin config are preserved by name. Locally-disabled plugins remain in
    the lock (disable is separate, per-machine state).
 
    Raises InstallerError if plugins/ holds no usable plugin.
    """
    root = project_root or _project_root()
    plugins_root = root / "plugins"
 
    found, skipped = _discover_local_manifests(plugins_root)
    if not found:
        raise InstallerError(
            f"No plugins with a plugin.toml found under {plugins_root}. "
            f"Add a plugin folder (or run `arc install <git-url>`) first."
        )
 
    # Carry source/branch/commit/config across by name so re-fetch / update work.
    prior = {e.name: e for e in _read_lock(root).plugins}
 
    entries: list[LockEntry] = []
    name_mismatches: list[tuple[str, str]] = []
    all_deps: list[str] = []
    seen_deps: set[str] = set()
 
    for dir_name, manifest in found:
        if manifest.name != dir_name:
            name_mismatches.append((dir_name, manifest.name))
        old = prior.get(manifest.name) or prior.get(dir_name)
        entries.append(LockEntry(
            name=manifest.name,
            version=manifest.version,
            entrypoint=_normalize_entrypoint(dir_name, manifest.entrypoint),
            provides=manifest.provides,
            requires=manifest.requires,
            load_order=manifest.load_order,
            critical=manifest.critical,
            config=old.config if old else {},
            source=old.source if old else None,
            branch=old.branch if old else None,
            commit=old.commit if old else None,
        ))
        for dep in manifest.dependencies:
            if dep not in seen_deps:
                seen_deps.add(dep)
                all_deps.append(dep)
 
    # Cosmetic file ordering (graph_hash sorts by name internally, so this only
    # affects how the lock reads in review): load_order, then name.
    entries.sort(key=lambda e: (e.load_order, e.name))
 
    PluginLoader.write_lock(root / "arc.lock", LockFile(plugins=entries))
 
    if install_deps and all_deps:
        _pip_install(all_deps)
 
    disabled = sorted(LocalState(root).disabled_set() & {e.name for e in entries})
 
    log.info("arc.build.done", plugins=len(entries), deps=len(all_deps),
             disabled=len(disabled), skipped=len(skipped))
    return BuildResult(
        plugins=[e.name for e in entries],
        installed_deps=all_deps,
        skipped_dirs=skipped,
        disabled=disabled,
        name_mismatches=name_mismatches,
    )