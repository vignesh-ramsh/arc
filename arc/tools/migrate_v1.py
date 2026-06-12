"""
arc.tools.migrate_v1
==================
One-shot migrator from a v1 arc.lock to the v2 "everything is plugin" format.

What it does:
  * remaps built-in entrypoints (arc.db.plugin -> arc.plugins.db.plugin, etc.)
  * adds provides/requires so the resolver can order the graph
  * inserts the http host plugin if absent
  * drops migration_order (now derived from the resolved graph)
  * preserves user plugins, adding a sensible requires=["db.session"]

It does NOT touch your database or schema JSON — those are forward-compatible.
Run:  python -m arc.tools.migrate_v1 /path/to/project/arc.lock
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# v1 entrypoint -> v2 entrypoint, with graph metadata.
BUILTINS = {
    "arc.db.plugin:DatabasePlugin": {
        "entrypoint": "arc.plugins.db.plugin:DatabasePlugin",
        "provides": ["db.engine", "db.session"],
        "requires": [],
        "load_order": 0,
        "critical": True,
    },
    "arc.api.plugin:ApiPlugin": {
        "entrypoint": "arc.plugins.api.plugin:ApiPlugin",
        "provides": ["http.router"],
        "requires": ["db.session"],
        "load_order": 50,
        "critical": True,
    },
}

HTTP_ENTRY = {
    "name": "http",
    "version": "1.0.0",
    "entrypoint": "arc.plugins.http.plugin:HttpPlugin",
    "provides": ["http.app"],
    "requires": [],
    "load_order": 90,
    "critical": True,
    "config": {},
}


def migrate(lock_path: Path) -> dict:
    old = json.loads(lock_path.read_text(encoding="utf-8"))
    new_plugins: list[dict] = []
    has_http = False

    for entry in old.get("plugins", []):
        ep = entry.get("entrypoint", "")
        if ep in BUILTINS:
            meta = BUILTINS[ep]
            new_plugins.append({
                "name": entry["name"],
                "version": entry.get("version", "1.0.0"),
                **meta,
                "config": entry.get("config", {}),
            })
        elif entry.get("name") == "http":
            has_http = True
            new_plugins.append(entry)
        else:
            # User plugin: assume it touches the DB.
            new_plugins.append({
                "name": entry["name"],
                "version": entry.get("version", "1.0.0"),
                "entrypoint": ep,
                "provides": entry.get("provides", []),
                "requires": entry.get("requires", ["db.session"]),
                "load_order": entry.get("load_order", 100),
                "critical": entry.get("critical", False),
                "config": entry.get("config", {}),
            })

    if not has_http:
        new_plugins.append(dict(HTTP_ENTRY))

    return {"arc_version": "2.0", "graph_hash": "", "plugins": new_plugins}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m arc.tools.migrate_v1 <path/to/arc.lock>")
        return 1
    lock_path = Path(argv[1])
    if not lock_path.exists():
        print(f"error: {lock_path} not found")
        return 1

    new_lock = migrate(lock_path)
    backup = lock_path.with_suffix(".lock.v1bak")
    lock_path.rename(backup)

    # Recompute graph_hash via the v2 loader for consistency.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from arc.kernel.loader import LockFile, PluginLoader

    PluginLoader.write_lock(lock_path, LockFile.model_validate(new_lock))
    print(f"\u2713 Migrated {lock_path}  (v1 backup at {backup.name})")
    print("  Review provides/requires, then run `arc doctor`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
