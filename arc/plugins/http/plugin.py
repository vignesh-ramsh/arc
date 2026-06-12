"""
arc.plugins.http
===============
The HTTP host plugin. It is the ONLY thing in Arc that imports Starlette, and
it is just a plugin — listed in arc.lock like any other.

It provides the ``http.app`` capability as a *factory*. The kernel fetches it
after every plugin has contributed, so by the time the Starlette app is built,
``http.routes`` and ``http.middleware`` are fully populated. A built-in
``/health`` route aggregates the lifecycle's health checks.

Swap this plugin out (or replace it with an aiohttp/FastAPI host) and the
kernel doesn't change a line — that's the point.
"""

from __future__ import annotations

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime

log = get_logger(__name__)


class HttpPlugin(Plugin):
    provides = ("http.app",)
    requires = ()
    load_order = 90
    critical = True
    description = "Starlette ASGI host (assembles contributed routes/middleware)"

    @property
    def name(self) -> str:
        return "http"

    @property
    def version(self) -> str:
        return "1.0.0"

    def setup(self, rt: Runtime) -> None:
        # Register a *factory* — built lazily once everything is contributed.
        rt.capabilities.provide(
            "http.app",
            factory=lambda: self._build_app(rt),
            source=self.name,
        )

    def _build_app(self, rt: Runtime):
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        routes = list(rt.extensions.get(Points.HTTP_ROUTES))
        middleware = list(rt.extensions.get(Points.HTTP_MIDDLEWARE))

        async def health(_request: Request) -> JSONResponse:
            results = await rt.lifecycle.health()
            payload = {name: {"status": r.status.value, "message": r.message}
                       for name, r in results.items()}
            ok = all(r.passed for r in results.values())
            return JSONResponse(
                {"status": "ok" if ok else "degraded", "plugins": payload},
                status_code=200 if ok else 503,
            )

        routes.append(Route("/health", health, methods=["GET"]))

        app = Starlette(
            debug=rt.config.app.debug,
            routes=routes,
            middleware=[Middleware(m) for m in middleware] if middleware else None,
        )
        log.info("arc.http.app_built", routes=len(routes), middleware=len(middleware))
        return app

    async def health_check(self) -> CheckResult:
        return CheckResult.ok("http host ready")
