"""
arc.plugins.relay.asgi
======================
Turns the registrar's RouteSpec list into a Starlette sub-application.
Each handler receives a ``Context``; the ASGI layer:

  1. Calls the handler.
  2. Receives a ``WriteResult`` (or a plain dict / Starlette Response).
  3. Sends the HTTP response immediately.
  4. Attaches ``WriteResult.post_commit`` as a Starlette ``BackgroundTask``
     so post-commit hooks (on_insert / on_update / on_delete) run AFTER the
     client has the response — the HTTP connection is not held open.

Error map:
  ValidationError  -> 422   NotFoundError -> 404
  ConflictError    -> 409   bad JSON      -> 400   anything else -> 500
"""

from __future__ import annotations

import inspect
import uuid as _uuid
from typing import Any

from arc.kernel.logger import get_logger
from plugins.relay.documents import ConflictError, NotFoundError, WriteResult
from plugins.relay.registry import Relay, ValidationError

log = get_logger("arc.plugin.relay.asgi")


class Context:
    """Everything a handler needs — no imports, no session juggling."""

    def __init__(self, request, gateway, data: dict, user: str | None):
        self.request   = request
        self.documents = gateway
        self.data      = data
        self.params    = dict(request.path_params)
        self.query     = dict(request.query_params)
        self.user      = user

    @classmethod
    async def build(cls, request, gateway) -> "Context":
        from arc.kernel.context import get_user

        data: dict = {}
        if request.method in ("POST", "PUT", "PATCH"):
            raw = await request.body()
            if raw:
                import json
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    raise _Abort(400, "invalid_json", "Request body is not valid JSON.")
                if not isinstance(parsed, dict):
                    raise _Abort(400, "invalid_body", "Body must be a JSON object.")
                data = parsed

        u = get_user()
        return cls(request, gateway, data, getattr(u, "id", None) if u else None)

    def uuid_param(self, name: str) -> str:
        """Validate a path param as a UUID; raises 404 automatically if invalid."""
        raw = self.params.get(name, "")
        try:
            _uuid.UUID(str(raw))
        except (ValueError, AttributeError, TypeError):
            raise _Abort(404, "not_found", f"'{name}' is not a valid id.")
        return raw


class _Abort(Exception):
    def __init__(self, status: int, code: str, detail: str):
        self.status = status
        self.code   = code
        self.detail = detail


class RelayASGI:
    """ASGI app mounted into http.routes. Router is built lazily on first
    request so route registration order is irrelevant at startup."""

    def __init__(self, registrar: Relay, gateway) -> None:
        self._reg     = registrar
        self._gw      = gateway
        self._router  = None

    async def __call__(self, scope, receive, send):
        if self._router is None:
            self._router = self._build_router()
        await self._router(scope, receive, send)

    def _build_router(self):
        from starlette.routing import Route, Router

        routes = [
            Route(
                spec.path,
                self._wrap(spec),
                methods=list(spec.methods),
                name=spec.name,
            )
            for spec in self._reg.routes
        ]
        log.info("arc.relay.router_built", routes=len(routes))
        return Router(routes=routes)

    def _wrap(self, spec):
        from starlette.background import BackgroundTask
        from starlette.responses import JSONResponse, Response

        async def endpoint(request):
            try:
                ctx    = await Context.build(request, self._gw)
                result = spec.handler(ctx)
                if inspect.isawaitable(result):
                    result = await result

                # ── Pass-through: handler returned a raw Starlette Response ──
                if isinstance(result, Response):
                    return result

                # ── WriteResult: send response NOW, run post-commit in bg ────
                if isinstance(result, WriteResult):
                    status = 201 if request.method == "POST" else 200
                    return JSONResponse(
                        _json_safe(result.data),
                        status_code=status,
                        background=BackgroundTask(result.post_commit),
                    )

                # ── Plain dict / scalar: simple response, no background work ─
                status = 201 if request.method == "POST" else 200
                return JSONResponse(_json_safe(result), status_code=status)

            except _Abort as a:
                return JSONResponse(
                    {"error": a.code, "detail": a.detail},
                    status_code=a.status,
                )
            except ValidationError as e:
                body: dict[str, Any] = {
                    "error":  "validation_error",
                    "detail": e.message,
                }
                if e.field:
                    body["field"] = e.field
                return JSONResponse(body, status_code=422)
            except NotFoundError as e:
                return JSONResponse(
                    {"error": "not_found", "detail": str(e)},
                    status_code=404,
                )
            except ConflictError as e:
                return JSONResponse(
                    {"error": "conflict", "detail": str(e)},
                    status_code=409,
                )
            except Exception as exc:
                log.error("arc.relay.handler_error", route=spec.name, error=str(exc))
                return JSONResponse({"error": "internal_error"}, status_code=500)

        return endpoint


def _json_safe(value):
    import datetime as _dt
    from decimal import Decimal

    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value