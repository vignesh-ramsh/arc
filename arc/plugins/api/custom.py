"""
arc.plugins.api.custom
====================
Decorator-driven custom endpoints for business logic that auto-CRUD can't
express (dashboards, aggregations, workflows). Declared in
``{plugin}/api/*.py``::

    from arc.plugins.api import api

    @api.get("/api/v1/hr/headcount")
    async def headcount(request):
        ...

The decorators register into a module-level registry; the api plugin reads it
and contributes the routes to ``http.routes``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class CustomRoute:
    path: str
    method: str
    handler: Callable


class _Registry:
    def __init__(self) -> None:
        self._routes: list[CustomRoute] = []

    def _register(self, method: str, path: str):
        def decorator(fn: Callable) -> Callable:
            self._routes.append(CustomRoute(path=path, method=method, handler=fn))
            return fn

        return decorator

    def get(self, path: str):
        return self._register("GET", path)

    def post(self, path: str):
        return self._register("POST", path)

    def patch(self, path: str):
        return self._register("PATCH", path)

    def delete(self, path: str):
        return self._register("DELETE", path)

    def routes(self) -> list[CustomRoute]:
        return list(self._routes)

    def clear(self) -> None:
        self._routes.clear()


# Singleton used by the @api.* decorators.
api = _Registry()
