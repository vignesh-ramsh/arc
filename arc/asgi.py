"""
arc.asgi
=======
Import-string ASGI entrypoint.

``Arc.run()`` passes an app *object* to uvicorn, which caps the server at a
single worker and disables ``--reload`` — the ``[server] workers`` key in
arc.toml was silently dead. uvicorn can only fork workers / hot-reload when
given an import string, so production deployments should serve this module:

    uvicorn arc.asgi:app --host 0.0.0.0 --port 8000 --workers 4
    uvicorn arc.asgi:app --reload          # development hot-reload

Each worker process imports this module fresh and builds its own Arc (its own
engine pool, its own lifecycle) — exactly what you want per-process.

Run from the project root (where arc.lock lives), or set the working
directory accordingly; the loader locates arc.lock by walking up from cwd.
"""

from __future__ import annotations

from arc.kernel.orchestrator import Arc


def create_app():
    """App factory — also usable as ``uvicorn --factory arc.asgi:create_app``."""
    app = Arc.shared().build()
    if app is None:
        raise RuntimeError(
            "No http plugin provided 'http.app' — nothing to serve. "
            "Add the http plugin to arc.lock or use Arc.run_headless()."
        )
    return app


app = create_app()