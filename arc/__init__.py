"""
arc
===
The flat application surface. After the kernel builds (orchestrator pass 3),
``import arc`` exposes every method contributed to ``Points.ARC_SURFACE`` by any
installed plugin:

    import arc

    rows = await arc.list("Employee")
    await arc.save("Employee", {...})
    await arc.set_cache("k", v, ttl=60)
    job = await arc.enqueue("send_email", to="a@b.c")

The surface is assembled once, at build time, from contributions — relay
contributes the document API (list/save/update/...) and the cache/queue/
scheduler facades; other plugins may contribute their own attributes. Accessing
``arc.<attr>`` before the kernel has built, or for an attribute no plugin
contributes, raises a clear error.

This module deliberately holds almost no logic — it is a thin proxy over the
``ArcSurface`` instance the orchestrator sets via ``_set_surface``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arc.surface import ArcSurface

# Set by arc.kernel.orchestrator.Arc.build() once every plugin's contribute()
# has run and the surface has been assembled.
_surface: "ArcSurface | None" = None


def _set_surface(surface: "ArcSurface") -> None:
    """Called by the orchestrator after building the surface. Idempotent-ish:
    a rebuild (e.g. dev reload) replaces the surface in place."""
    global _surface
    _surface = surface


def _get_surface() -> "ArcSurface":
    if _surface is None:
        raise RuntimeError(
            "arc.* is not available yet — the kernel has not finished building. "
            "Access arc.<method> from inside a request handler, hook, task, or "
            "scheduled job (i.e. at run time), not at module import time."
        )
    return _surface


def __getattr__(name: str) -> Any:
    # PEP 562 module-level __getattr__: only invoked for names not already
    # defined at module scope, so the real functions/vars above are untouched.
    if name.startswith("_"):
        raise AttributeError(f"module 'arc' has no attribute {name!r}")
    return getattr(_get_surface(), name)


def __dir__() -> list[str]:
    base = ["_set_surface", "_get_surface"]
    if _surface is not None:
        return base + _surface.attrs()
    return base


__all__: list[str] = []   # everything is dynamic via __getattr__