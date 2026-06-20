"""
arc.kernel.registry
===================
The extension registry — how plugins contribute to a shared runtime without
the kernel (or each other) knowing their names.

Instead of the kernel calling ``ApiPlugin`` to collect routes, every plugin
``contribute``s into named *extension points*. The kernel and other plugins
``get`` from those points. This is what removes all special-casing: the http
host asks "who filled ``http.routes``?", not "let me call the api plugin".

Well-known points (any plugin may define more):

    http.routes        Starlette Route objects
    http.middleware    Starlette Middleware objects
    cli.commands       Typer apps / commands to mount under `arc`
    db.schema_sources  objects exposing table schemas to migrate
    resource.sources   api Resource declarations to auto-CRUD
    health.checks      extra async () -> CheckResult callables
    arc.surface        {attr_name: callable} maps merged into the flat ``arc``
                       object (see arc.surface.build_arc_surface). Many plugins
                       may contribute; duplicate attribute names raise at boot.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class Contribution:
    point: str
    value: Any
    source: str  # plugin that contributed it


# Canonical point names — string constants so typos surface as attribute errors.
class Points:
    HTTP_ROUTES = "http.routes"
    HTTP_MIDDLEWARE = "http.middleware"
    CLI_COMMANDS = "cli.commands"
    DB_SCHEMA_SOURCES = "db.schema_sources"
    RESOURCE_SOURCES = "resource.sources"
    HEALTH_CHECKS = "health.checks"
    ARC_SURFACE = "arc.surface"


class ExtensionRegistry:
    def __init__(self) -> None:
        self._points: dict[str, list[Contribution]] = defaultdict(list)

    def contribute(self, point: str, value: Any, *, source: str = "") -> None:
        self._points[point].append(Contribution(point, value, source))

    def get(self, point: str) -> list[Any]:
        """All contributed values for *point*, in contribution order."""
        return [c.value for c in self._points.get(point, [])]

    def items(self, point: str) -> list[Contribution]:
        """Contributions with their source plugin attached."""
        return list(self._points.get(point, []))

    def points(self) -> list[str]:
        return sorted(self._points)

    def __iter__(self) -> Iterator[Contribution]:
        for contributions in self._points.values():
            yield from contributions