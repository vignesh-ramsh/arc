"""
arc.plugins.http
===============
The HTTP host plugin. It is the ONLY thing in Arc that imports Starlette, and
it is just a plugin — listed in arc.lock like any other.

It provides the ``http.app`` capability as a *factory*. The kernel fetches it
after every plugin has contributed, so by the time the Starlette app is built,
``http.routes`` and ``http.middleware`` are fully populated. A built-in
``/health`` route aggregates the lifecycle's plugin checks AND every callable
contributed to the ``health.checks`` extension point (previously documented
but never consumed).

The ``[plugins.http]`` config (``cors_origins`` / ``cors_methods``) is now
honored: when ``cors_origins`` is non-empty, Starlette's CORSMiddleware is
installed ahead of contributed middleware. Previously the keys existed in
arc.toml but were silently ignored.

Swap this plugin out (or replace it with an aiohttp/FastAPI host) and the
kernel doesn't change a line — that's the point.
"""

from __future__ import annotations

import asyncio

from arc.kernel.contracts import CheckResult
from arc.kernel.logger import get_logger
from arc.kernel.plugin import Plugin
from arc.kernel.registry import Points
from arc.kernel.runtime import Runtime

log = get_logger(__name__)

EXTENSION_CHECK_TIMEOUT = 5.0


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
        from starlette.middleware.cors import CORSMiddleware
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        routes = list(rt.extensions.get(Points.HTTP_ROUTES))
        contributed_mw = list(rt.extensions.get(Points.HTTP_MIDDLEWARE))
        extra_checks = list(rt.extensions.get(Points.HEALTH_CHECKS))

        async def health(_request: Request) -> JSONResponse:
            # Plugin checks (concurrent + timed out inside the lifecycle).
            results = await rt.lifecycle.health()

            # Extension-point checks — the documented contract: any plugin
            # may contribute ``async () -> CheckResult`` to health.checks.
            async def probe(fn) -> CheckResult:
                try:
                    return await asyncio.wait_for(fn(), EXTENSION_CHECK_TIMEOUT)
                except asyncio.TimeoutError:
                    return CheckResult.fail(
                        f"check timed out after {EXTENSION_CHECK_TIMEOUT:.0f}s"
                    )
                except Exception as exc:
                    return CheckResult.fail(str(exc))

            if extra_checks:
                extra_results = await asyncio.gather(*(probe(fn) for fn in extra_checks))
                for i, r in enumerate(extra_results):
                    name = getattr(extra_checks[i], "__name__", f"check_{i}")
                    results[f"ext:{name}"] = r

            payload = {name: {"status": r.status.value, "message": r.message}
                       for name, r in results.items()}
            ok = all(r.passed for r in results.values())
            return JSONResponse(
                {"status": "ok" if ok else "degraded", "plugins": payload},
                status_code=200 if ok else 503,
            )

        routes.append(Route("/health", health, methods=["GET"]))

        # ── Middleware: CORS from [plugins.http] config, then contributions ──
        middleware: list = []
        cfg = dict(rt.plugin_config or {})
        cors_origins = list(cfg.get("cors_origins") or [])
        if cors_origins:
            middleware.append(Middleware(
                CORSMiddleware,
                allow_origins=cors_origins,
                allow_methods=list(cfg.get("cors_methods")
                                   or ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]),
                allow_headers=list(cfg.get("cors_headers") or ["*"]),
                allow_credentials=bool(cfg.get("cors_credentials", False)),
            ))
        middleware.extend(Middleware(m) for m in contributed_mw)

        app = Starlette(
            debug=rt.config.app.debug,
            routes=routes,
            middleware=middleware or None,
        )
        log.info(
            "arc.http.app_built",
            routes=len(routes),
            middleware=len(middleware),
            cors=bool(cors_origins),
            extra_health_checks=len(extra_checks),
        )
        return app

    async def health_check(self) -> CheckResult:
        return CheckResult.ok("http host ready")