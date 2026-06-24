"""
plugins.admin
=============
A superuser-only admin panel for Arc. It serves a single-page UI at ``/admin``
and a small group of privileged JSON routes under ``/api/v1/admin/*`` that the
UI calls. The plugin adds NO new database surface — it composes the existing
``arc`` document gateway, the ``auth.context`` service, and the ``arc db migrate``
CLI.

Why a context holder
--------------------
Route modules are auto-discovered and imported by relay, so they cannot take
constructor arguments. They reference the process-wide ``admin_ctx`` singleton,
which ``AdminPlugin.setup()`` binds to the capabilities admin needs. This mirrors
how relay binds ``arc`` and how authn binds ``auth_service``.

``arc`` (the DB gateway) is imported directly from ``plugins.relay`` inside the
route bodies — the same sanctioned surface hrms/sales handlers use. Only the
cross-plugin ``auth.context`` capability is carried on the holder, because admin
must not import authn's internals.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── sys.path fix ──────────────────────────────────────────────────────────────
# relay's discovery constructs import paths as "{plugin_name}.routes.{file}"
# (e.g. "admin.routes.migrate"). When arc runs from the plugins/ directory,
# the module_prefix collapses to "" so relay tries to import "admin.routes.*"
# as a top-level package — which requires plugins/ to be on sys.path.
#
# This __init__.py is imported during kernel graph construction, BEFORE
# relay.contribute() fires discovery — the only place early enough to fix this.
#
# Path(__file__).parent        → .../plugins/admin/
# Path(__file__).parent.parent → .../plugins/   ← must be on sys.path
_plugins_dir = str(Path(__file__).parent.parent.resolve())
if _plugins_dir not in sys.path:
    sys.path.insert(0, _plugins_dir)
# ─────────────────────────────────────────────────────────────────────────────


class AdminContext:
    """Process-wide holder for the capabilities and config admin routes need.
    Bound once in AdminPlugin.setup(); read at request time by the handlers."""

    def __init__(self) -> None:
        self._auth = None
        self._queue = None                       # redix queue.client — None if absent
        self._project_root: Path | None = None
        self._migrate_cmd: list[str] | None = None
        self._row_blocklist: frozenset[str] = frozenset()

    def bind(self, *, auth, project_root: Path, migrate_cmd: list[str],
             row_blocklist: frozenset[str], queue=None) -> None:
        self._auth = auth
        self._queue = queue
        self._project_root = project_root
        self._migrate_cmd = list(migrate_cmd)
        self._row_blocklist = row_blocklist

    def _require_bound(self) -> None:
        if self._auth is None or self._project_root is None:
            raise RuntimeError(
                "admin is not initialised — AdminPlugin.setup() has not run. "
                "Ensure 'admin' is registered in arc.lock (run `arc build`).")

    @property
    def auth(self):
        self._require_bound()
        return self._auth

    @property
    def queue(self):
        """The redix queue.client, or None when redix is absent. Callers must
        handle None (the Queue panel shows an 'unavailable' state)."""
        return self._queue

    @property
    def project_root(self) -> Path:
        self._require_bound()
        return self._project_root  # type: ignore[return-value]

    @property
    def migrate_cmd(self) -> list[str]:
        self._require_bound()
        return list(self._migrate_cmd or [])

    @property
    def row_blocklist(self) -> frozenset[str]:
        return self._row_blocklist


# Process-wide singleton (created at import; bound in AdminPlugin.setup()).
admin_ctx = AdminContext()

__all__ = ["AdminContext", "admin_ctx"]