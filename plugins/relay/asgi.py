"""
plugins.relay.asgi
==================
Turns RouteSpecs into a Starlette sub-app and runs the request pipeline:

    before_req(ctx)                      may raise / return a Response to stop
      → [Auth / RateLimit guards]        STUB — declared on the route, enforced
                                         by the Phase-4 auth middleware
        → handler(ctx)
            → validate → before_* → WRITE → after_* → COMMIT   (in the gateway)
      → after_req(ctx, response)         ALWAYS runs (even on error)
    → response sent
        ⤷ on_change / on_commit / on_rollback   (background, AFTER the response)

Error envelope: any RelayError → its status + ``{"error": {...}}``; anything
else → 500. Request bodies are capped at ``arc.max_body_bytes`` (413). Post-commit
hooks queued by the gateway run as a BackgroundTask so a failing post-commit hook
can never affect the response that already went out.
"""

from __future__ import annotations

import inspect
import json

from arc.kernel.logger import get_logger
from plugins.relay.documents import _post_commit_queue
from plugins.relay.errors import BadJSON, PayloadTooLarge, RelayError
from plugins.relay.registry import Relay

log = get_logger("arc.plugin.relay.asgi")


class Context:
    """Everything a handler needs. ``ctx.arc`` is the single DB API."""

    def __init__(self, request, arc, data: dict, user: str | None):
        self.request = request
        self.arc = arc
        self.data = data
        self.params = dict(request.path_params)
        self.query = dict(request.query_params)
        self.user = user
        self.response = None  # populated before after_req runs

    @classmethod
    async def build(cls, request, arc) -> "Context":
        from arc.kernel.context import get_user

        data: dict = {}
        if request.method in ("POST", "PUT", "PATCH"):
            cap = int(getattr(arc, "max_body_bytes", 0) or 0)

            # Cheap pre-read reject on a declared Content-Length (a chunked or
            # forged body may omit it — the post-read check below is the backstop).
            if cap:
                cl = request.headers.get("content-length")
                if cl is not None:
                    try:
                        declared = int(cl)
                    except ValueError:
                        declared = -1
                    if declared > cap:
                        raise PayloadTooLarge(
                            f"Request body exceeds the {cap}-byte limit.")

            raw = await request.body()
            if cap and len(raw) > cap:
                raise PayloadTooLarge(f"Request body exceeds the {cap}-byte limit.")

            if raw:
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    raise BadJSON("Request body is not valid JSON.")
                if not isinstance(parsed, dict):
                    raise BadJSON("Body must be a JSON object.")
                data = parsed
        u = get_user()
        return cls(request, arc, data, getattr(u, "id", None) if u else None)


class RelayASGI:
    """ASGI app mounted into http.routes. The router is built lazily on first
    request, so route registration order is irrelevant at startup.

    Routes are registered with their full absolute paths (e.g.
    ``/api/v1/hrms/employees``) so code is readable. ``base_path`` is the
    prefix that Starlette's ``Mount`` strips before passing the request to this
    sub-app, so the internal Starlette Router must see paths WITHOUT that prefix
    (e.g. ``/hrms/employees``). This class strips it automatically in
    ``_build_router`` so both layers stay in sync.
    """

    def __init__(self, registrar: Relay, arc, *, base_path: str = "") -> None:
        self._reg = registrar
        self._arc = arc
        self._base = base_path.rstrip("/")   # e.g. "/api/v1" — never ends with /
        self._router = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        if self._router is None:
            self._router = self._build_router()
        await self._router(scope, receive, send)

    def _build_router(self):
        from starlette.routing import Route, Router

        routes = []
        for spec in self._reg.routes:
            path = spec.path
            if self._base and path.startswith(self._base):
                path = path[len(self._base):] or "/"
            elif not path.startswith("/"):
                path = "/" + path
            routes.append(
                Route(path, self._wrap(spec), methods=list(spec.methods), name=spec.name)
            )
        log.info("arc.relay.router_built", routes=len(routes), base=self._base)
        return Router(routes=routes)

    def _wrap(self, spec):
        from starlette.background import BackgroundTask
        from starlette.responses import JSONResponse, Response, StreamingResponse

        async def endpoint(request):
            token = _post_commit_queue.set([])
            ctx = None
            response = None
            try:
                ctx = await Context.build(request, self._arc)

                # before_req — may short-circuit by returning / raising a Response.
                for fn in self._reg.req_hooks("before_req"):
                    out = fn(ctx)
                    if inspect.isawaitable(out):
                        out = await out
                    if isinstance(out, Response):
                        response = out
                        break

                # [Auth / RateLimit guards run here once Phase 4 lands —
                #  they read spec.roles / spec.rate_limit. Declared, not yet enforced.]

                if response is None:
                    if spec.stream:
                        response = StreamingResponse(
                            _stream_body(spec.handler, ctx),
                            media_type="application/x-ndjson",
                        )
                    else:
                        result = spec.handler(ctx)
                        if inspect.isawaitable(result):
                            result = await result
                        response = result if isinstance(result, Response) \
                            else JSONResponse(_json_safe(result))

            except RelayError as exc:
                response = JSONResponse(exc.to_dict(), status_code=exc.status)
            except Exception as exc:  # noqa: BLE001
                log.error("arc.relay.unhandled", path=spec.path, error=str(exc))
                response = JSONResponse(
                    {"error": {"source": "relay", "code": "internal_error",
                               "message": "Internal server error.", "status": 500}},
                    status_code=500,
                )
            finally:
                # after_req ALWAYS runs, even on error / short-circuit.
                if ctx is not None:
                    ctx.response = response
                    for fn in self._reg.req_hooks("after_req"):
                        try:
                            out = fn(ctx, response)
                            if inspect.isawaitable(out):
                                await out
                        except Exception as exc:  # noqa: BLE001
                            log.error("arc.relay.after_req_error", error=str(exc))

            # Drain the post-commit queue AFTER the response (background).
            queued = _post_commit_queue.get() or []
            _post_commit_queue.reset(token)
            if queued and not spec.stream:
                response.background = BackgroundTask(_run_post_commit, queued)
            return response

        return endpoint


async def _stream_body(handler, ctx):
    """Adapt a handler async-generator into NDJSON chunks."""
    result = handler(ctx)
    if inspect.isasyncgen(result):
        async for row in result:
            yield (json.dumps(_json_safe(row), default=str) + "\n").encode()
    else:
        if inspect.isawaitable(result):
            result = await result
        for row in result or []:
            yield (json.dumps(_json_safe(row), default=str) + "\n").encode()


async def _run_post_commit(queued: list) -> None:
    for coro in queued:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            log.error("arc.relay.post_commit_error", error=str(exc))


def _json_safe(value):
    """Recursively coerce non-JSON-serialisable DB types to safe equivalents.
    asyncpg returns UUID objects, Decimal, datetime, etc. — none of which
    stdlib json.dumps handles. Applied to every handler result before it
    reaches JSONResponse so callers never have to think about it."""
    import datetime as _dt
    import uuid as _uuid
    from decimal import Decimal

    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


__all__ = ["Context", "RelayASGI"]