"""
arc.kernel.watcher
=================
Development-only auto-reload for ``plugins/``. Event-based (inotify / FSEvents
via ``watchfiles``) — near-zero idle CPU, no polling.

Gated twice: it only runs when ``[app] environment == "development"`` AND the
operator passed ``arc run --reload``. It is never active in any other
environment.

On a relevant change it restarts the server process cleanly (a fresh Python
process = a correct, fresh kernel build). It classifies what changed:

  *.py / resources/*.json / hooks.json / router.json  → restart (safe)
  schemas/** / patches/**                             → restart + MIGRATE
                                                         reminder (never auto-DDL)
  plugin.toml / added or removed plugin folder        → restart + run-doctor
                                                         notice (graph changed)

``watchfiles`` is an optional dependency (the ``[dev]`` extra). It is imported
lazily so production installs never need it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from arc.kernel.logger import get_logger

log = get_logger(__name__)


# Module-level so watchfiles.run_process can spawn it in a child process.
def _serve_target(host: str, port: int) -> None:
    """Runs in the child process: build a fresh Arc and serve."""
    from arc.kernel.orchestrator import Arc

    Arc.reset_shared()
    Arc.shared().run(host=host, port=port)


def _classify(changes) -> None:
    """Print guidance based on what changed. Runs in the parent before restart."""
    paths = [Path(p) for _, p in changes]
    schema_touched   = any(
        "schemas" in p.parts or "patches" in p.parts for p in paths
    )
    manifest_touched = any(p.name == "plugin.toml" for p in paths)

    changed = ", ".join(sorted({p.name for p in paths})[:6])
    log.info("arc.reload", changed=changed)

    if schema_touched:
        print(
            "  ⚠ schema/patch change detected — DDL is NOT applied automatically.\n"
            "    Run `arc db migrate` (or `arc db plan`) to apply it.",
            file=sys.stderr,
        )
    if manifest_touched:
        print(
            "  ⚠ plugin.toml changed — the plugin graph may have changed.\n"
            "    Run `arc doctor` to re-validate.",
            file=sys.stderr,
        )


def run_with_reload(host: str, port: int, project_root: Path) -> None:
    """Watch project_root/plugins and restart the server on change."""
    try:
        from watchfiles import DefaultFilter, run_process
    except ImportError:
        print(
            "Auto-reload needs the 'watchfiles' package, which ships in the dev "
            "extra:\n    pip install 'arc[dev]'\n"
            "Serving once without reload instead.",
            file=sys.stderr,
        )
        _serve_target(host, port)
        return

    plugins_dir = project_root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)

    log.info("arc.reload.watching", path=str(plugins_dir))
    print(f"↻ Dev reload active — watching {plugins_dir}", file=sys.stderr)

    # run_process spawns _serve_target in a child process and restarts it on
    # change. DefaultFilter already ignores .git, __pycache__, *.pyc, etc.
    run_process(
        plugins_dir,
        target=_serve_target,
        args=(host, port),
        callback=_classify,
        watch_filter=DefaultFilter(),
        debounce=300,
    )