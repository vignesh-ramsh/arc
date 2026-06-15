"""
arc.kernel.state
================
Local, per-machine runtime state that must NEVER be committed. Lives under
``.arc/state/`` (which is gitignored), so anything here affects only this
machine/server — exactly what ``arc disable`` needs.

Currently tracks the set of locally-disabled plugins. ``arc.lock`` stays the
record of *what is installed*; this file records *what this machine has turned
off*. The loader reads the lock for the full set, then subtracts this set
before resolving the graph.
"""

from __future__ import annotations

import json
from pathlib import Path

STATE_SUBDIR  = ".arc/state"
DISABLED_FILE = "disabled.json"


class LocalState:
    """Read/write ``.arc/state/disabled.json`` for one project root."""

    def __init__(self, project_root: Path) -> None:
        self._root          = Path(project_root)
        self._dir           = self._root / ".arc" / "state"
        self._disabled_path = self._dir / DISABLED_FILE

    # ── internal ──────────────────────────────────────────────────────
    def _read(self) -> set[str]:
        try:
            raw = json.loads(self._disabled_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()
        items = raw.get("disabled", []) if isinstance(raw, dict) else raw
        return {str(x) for x in items}

    def _write(self, names: set[str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._disabled_path.write_text(
            json.dumps({"disabled": sorted(names)}, indent=2) + "\n",
            encoding="utf-8",
        )

    # ── public ────────────────────────────────────────────────────────
    def disabled_set(self) -> set[str]:
        """The set of plugin names disabled on this machine."""
        return self._read()

    def is_disabled(self, name: str) -> bool:
        return name in self._read()

    def disable(self, name: str) -> bool:
        """Disable a plugin locally.

        Returns True if state changed, False if it was already disabled
        (a no-op — the caller treats False as 'already in that state', not
        an error).
        """
        names = self._read()
        if name in names:
            return False
        names.add(name)
        self._write(names)
        return True

    def enable(self, name: str) -> bool:
        """Enable a plugin locally.

        Returns True if state changed, False if it was already enabled
        (a no-op, not an error).
        """
        names = self._read()
        if name not in names:
            return False
        names.discard(name)
        self._write(names)
        return True