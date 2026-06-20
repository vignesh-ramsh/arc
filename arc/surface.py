"""
arc.surface
===========
The flat ``arc`` namespace, assembled from every ``ARC_SURFACE`` contribution.

Plugins contribute a mapping of ``{attribute_name: callable}`` to the
``Points.ARC_SURFACE`` extension point in their ``contribute()``. After every
plugin's ``contribute()`` has run, the orchestrator calls
``build_arc_surface(rt)`` once; the resulting ``ArcSurface`` instance is what a
business plugin gets from ``import arc``.

    # in relay's contribute():
    rt.extensions.contribute(
        Points.ARC_SURFACE,
        {"list": _arc.list, "save": _arc.save, "get_cache": _arc.get_cache, ...},
        source="relay",
    )

    # in a business plugin module:
    import arc
    rows = await arc.list("Employee")
    await arc.set_cache("k", v, ttl=60)

Collision behavior
------------------
Two plugins contributing the same attribute name raise ``CapabilityError`` at
boot — loud and early, the same posture as ``Capabilities.provide()``'s
duplicate check. Swapping which plugin owns ``arc.<attr>`` is a one-line change
in that plugin's contribution dict; every call site stays unchanged.

Why an extension point and not a capability
-------------------------------------------
A capability is exactly-one-provider-per-name by construction. ``arc.*`` is many
small contributions (individual methods) merging into one object, contributed by
however many plugins choose to extend it — that's the extension-point shape, with
the kernel doing the merge and the conflict check centrally, once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from arc.kernel.exceptions import CapabilityError
from arc.kernel.logger import get_logger
from arc.kernel.registry import Points

if TYPE_CHECKING:
    from arc.kernel.runtime import Runtime

log = get_logger("arc.surface")


class ArcSurface:
    """Flat namespace assembled from every ``ARC_SURFACE`` contribution.

    Attributes are set dynamically by ``build_arc_surface``. Accessing an
    attribute that no plugin contributed raises ``AttributeError`` with a hint
    listing what *is* available, so a typo or a missing plugin is obvious.
    """

    def __init__(self) -> None:
        # Names of attributes that were actually contributed (for error hints
        # and introspection). Stored under a dunder so it can't collide with a
        # contributed attribute name.
        object.__setattr__(self, "__surface_attrs__", set())

    def _register(self, attr: str, fn: Callable) -> None:
        setattr(self, attr, fn)
        self.__surface_attrs__.add(attr)

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal attribute lookup fails — i.e. the attribute
        # was never contributed. (Contributed attrs are real instance attrs.)
        available = sorted(object.__getattribute__(self, "__surface_attrs__"))
        raise AttributeError(
            f"arc has no attribute {name!r}. "
            f"No installed plugin contributes it to ARC_SURFACE. "
            f"Available: {available}"
        )

    def attrs(self) -> list[str]:
        """Sorted list of every contributed attribute name."""
        return sorted(self.__surface_attrs__)


def build_arc_surface(rt: "Runtime") -> ArcSurface:
    """Merge every ``Points.ARC_SURFACE`` contribution into one ``ArcSurface``.

    Runs once, after all plugins' ``contribute()`` calls. Raises
    ``CapabilityError`` if two plugins contribute the same attribute name.
    """
    surface = ArcSurface()
    seen: dict[str, str] = {}  # attr -> source plugin

    for contribution in rt.extensions.items(Points.ARC_SURFACE):
        mapping = contribution.value
        source = contribution.source or "?"
        if not isinstance(mapping, dict):
            raise CapabilityError(
                f"ARC_SURFACE contribution from '{source}' must be a dict of "
                f"{{attr: callable}}, got {type(mapping).__name__}.",
                code="arc.surface.bad_contribution",
            )
        for attr, fn in mapping.items():
            if attr in seen:
                raise CapabilityError(
                    f"arc.{attr} contributed by both '{seen[attr]}' and "
                    f"'{source}'. Each arc.* attribute must have exactly one "
                    f"owner; rename one or remove the duplicate.",
                    code="arc.surface.duplicate",
                )
            if not callable(fn):
                raise CapabilityError(
                    f"arc.{attr} from '{source}' is not callable "
                    f"({type(fn).__name__}).",
                    code="arc.surface.not_callable",
                )
            seen[attr] = source
            surface._register(attr, fn)

    log.info("arc.surface.built", attrs=surface.attrs(),
             sources=sorted(set(seen.values())))
    return surface