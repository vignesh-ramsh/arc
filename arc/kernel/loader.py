from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from arc.kernel.exceptions import LockFileError, PluginLoadError, PluginNotFoundError
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin

log = get_logger(__name__)

LOCK_FILENAME = "arc.lock"
ARC_LOCK_VERSION = "2.0"


class LockEntry(BaseModel):
    name: str
    version: str = "1.0.0"
    entrypoint: str
    provides: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    load_order: int = 100
    critical: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    # ── add these three ──
    source: str | None = None      # git URL the plugin was installed from
    branch: str | None = None      # branch / tag
    commit: str | None = None      # pinned commit hash

    @field_validator("entrypoint")
    @classmethod
    def _valid_entrypoint(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError(f"Entrypoint '{v}' must be 'module:ClassName'.")
        return v


class LockFile(BaseModel):
    arc_version: str = ARC_LOCK_VERSION
    graph_hash: str = ""
    plugins: list[LockEntry] = Field(default_factory=list)


# ── Walk-up + sys.path injection ────────────────────────────────────────
def find_lock_file(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / LOCK_FILENAME
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            raise LockFileError(
                f"{LOCK_FILENAME} not found searching up from "
                f"'{start or Path.cwd()}'. Run 'arc init' to create a project.",
                code="arc.lock.not_found",
            )
        current = parent


def inject_project_root(project_root: Path) -> None:
    root = str(project_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)


# ── Loader ──────────────────────────────────────────────────────────────
class PluginLoader:
    def __init__(self, lock_path: Path | None = None) -> None:
        self._explicit = lock_path
        self._lock_path: Path | None = lock_path

    @property
    def lock_path(self) -> Path:
        if self._explicit is not None:
            return self._explicit
        if self._lock_path is None or not self._lock_path.exists():
            self._lock_path = find_lock_file()
        return self._lock_path

    @property
    def project_root(self) -> Path:
        return self.lock_path.parent

    def read_lock(self) -> LockFile:
        path = self.lock_path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LockFileError(f"{path} not found.", code="arc.lock.not_found") from exc
        except json.JSONDecodeError as exc:
            raise LockFileError(
                f"{path} is not valid JSON: {exc}", code="arc.lock.invalid_json"
            ) from exc
        return LockFile.model_validate(raw)

    def load_all(self) -> list[Plugin]:
        inject_project_root(self.project_root)
        lock = self.read_lock()
        return [self._load_entry(e) for e in lock.plugins]

    def load_one(self, name: str) -> Plugin:
        inject_project_root(self.project_root)
        for entry in self.read_lock().plugins:
            if entry.name == name:
                return self._load_entry(entry)
        raise PluginNotFoundError(
            f"Plugin '{name}' is not listed in {self.lock_path}.",
            code="arc.plugin.not_found",
        )

    @staticmethod
    def _load_entry(entry: LockEntry) -> Plugin:
        module_path, class_name = entry.entrypoint.split(":", 1)
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise PluginLoadError(
                f"Cannot import '{module_path}' for plugin '{entry.name}': {exc}",
                code="arc.plugin.import_failed",
            ) from exc
        cls = getattr(module, class_name, None)
        if cls is None:
            raise PluginLoadError(
                f"Class '{class_name}' not found in '{module_path}'.",
                code="arc.plugin.class_missing",
            )
        if not (isinstance(cls, type) and issubclass(cls, Plugin)):
            raise PluginLoadError(
                f"'{entry.entrypoint}' does not subclass arc.kernel.plugin.Plugin.",
                code="arc.plugin.bad_type",
            )
        try:
            instance = cls()
        except Exception as exc:
            raise PluginLoadError(
                f"Failed to instantiate '{entry.name}': {exc}",
                code="arc.plugin.instantiate_failed",
            ) from exc
        # Lock-declared graph metadata wins over class defaults so the lock
        # stays the single source of truth for the dependency graph.
        if entry.provides:
            instance.__class__.provides = tuple(entry.provides)  # type: ignore[misc]
        if entry.requires:
            instance.__class__.requires = tuple(entry.requires)  # type: ignore[misc]
        return instance

    # ── Lock writers (used by `arc init` / `arc new-plugin`) ────────────
    @staticmethod
    def write_lock(path: Path, lock: LockFile) -> None:
        lock = lock.model_copy(update={"graph_hash": PluginLoader.graph_hash(lock)})
        path.write_text(
            json.dumps(lock.model_dump(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def graph_hash(lock: LockFile) -> str:
        payload = json.dumps(
            [
                {
                    "name": e.name,
                    "version": e.version,
                    "entrypoint": e.entrypoint,
                    "provides": sorted(e.provides),
                    "requires": sorted(e.requires),
                }
                for e in sorted(lock.plugins, key=lambda x: x.name)
            ],
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()
