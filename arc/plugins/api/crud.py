"""
arc.plugins.api.crud
==================
Builds Starlette routes for a ``Resource``. Handlers obtain a session through
the ``db.session`` capability (a context-manager callable) rather than
importing the db plugin — api depends on the *capability*, not the module.

Skeleton coverage: list (paginated), get-by-id, create. The v1 feature set
(PATCH, soft delete to _trash, 14 filter operators, ETag/304, cursor
pagination, field-level permissions) plugs in at the marked seams without
changing the route signatures.

Hardening in this revision:
  * _json_safe handles Decimal, bytes, time, timedelta (a Decimal column
    previously crashed every list/get with a serialization TypeError).
  * ?limit= / ?offset= are validated → 400 instead of a 500 on ?limit=abc;
    offset is clamped to >= 0.
  * GET /{id} validates the UUID → 404, instead of an asyncpg error → 500.
  * POST validates the JSON body → 400 on malformed JSON / non-object.
  * Unique/FK violations surface as 409 conflict, not a 500.
  * created_by / updated_by are populated from UserContext when present.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Callable

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from arc.kernel.context import get_user
from arc.plugins.api.resource import Resource


def _parse_int(raw: str | None, default: int) -> int | None:
    """Parse a query-string integer; None signals a validation error."""
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def build_routes(resource: Resource, session_cm: Callable):
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    cols = ", ".join(f'"{c}"' for c in resource.read_columns)
    table = f'"{resource.table}"'

    def _serialize(row) -> dict:
        return {k: _json_safe(v) for k, v in row._mapping.items()}

    async def list_view(request: Request) -> JSONResponse:
        limit = _parse_int(request.query_params.get("limit"), resource.page_size)
        offset = _parse_int(request.query_params.get("offset"), 0)
        if limit is None or offset is None:
            return JSONResponse(
                {"error": "invalid_pagination",
                 "detail": "limit and offset must be integers"},
                status_code=400,
            )
        limit = max(1, min(limit, resource.max_page_size))
        offset = max(0, offset)
        # SEAM: filter compiler (?field=op:value) builds the WHERE clause here.
        sql = text(
            f'SELECT {cols} FROM {table} '
            f"ORDER BY updated_at DESC, id DESC LIMIT :limit OFFSET :offset"
        )
        async with session_cm() as session:
            result = await session.execute(sql, {"limit": limit, "offset": offset})
            rows = [_serialize(r) for r in result]
        return JSONResponse({"data": rows, "limit": limit, "offset": offset})

    async def get_view(request: Request) -> JSONResponse:
        rid = request.path_params["id"]
        try:
            rid = str(_uuid.UUID(str(rid)))
        except (ValueError, AttributeError, TypeError):
            # A malformed id can never match a row — 404, not a driver 500.
            return JSONResponse({"error": "not_found"}, status_code=404)
        sql = text(f'SELECT {cols} FROM {table} WHERE id = :id')
        async with session_cm() as session:
            row = (await session.execute(sql, {"id": rid})).first()
        if row is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse({"data": _serialize(row)})

    async def create_view(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "invalid_json", "detail": "body must be a JSON object"},
                status_code=400,
            )

        payload = {k: body.get(k) for k in resource.writable if k in body}
        if not payload:
            return JSONResponse({"error": "no_writable_fields"}, status_code=422)

        # System audit columns from the request's UserContext (set by the
        # auth middleware in Phase 4; None until then).
        user = get_user()
        if user is not None and user.id:
            payload["created_by"] = user.id
            payload["updated_by"] = user.id

        names = ", ".join(f'"{k}"' for k in payload)
        binds = ", ".join(f":{k}" for k in payload)
        sql = text(
            f'INSERT INTO {table} ({names}) VALUES ({binds}) RETURNING {cols}'
        )
        try:
            async with session_cm() as session:
                row = (await session.execute(sql, payload)).first()
        except IntegrityError as exc:
            # Unique-key or FK violation — the client's data conflicts.
            return JSONResponse(
                {"error": "conflict", "detail": _pg_detail(exc)},
                status_code=409,
            )
        except DBAPIError as exc:
            # Type mismatches etc. — bad input, not a server fault.
            return JSONResponse(
                {"error": "invalid_value", "detail": _pg_detail(exc)},
                status_code=422,
            )
        return JSONResponse({"data": _serialize(row)}, status_code=201)

    return [
        Route(resource.path, list_view, methods=["GET"]),
        Route(resource.path, create_view, methods=["POST"]),
        Route(resource.path + "/{id}", get_view, methods=["GET"]),
    ]


def _pg_detail(exc: Exception) -> str:
    """Short, single-line driver message safe to return to API clients."""
    orig = getattr(exc, "orig", None)
    msg = str(orig) if orig is not None else str(exc)
    return msg.splitlines()[0][:300]


def _json_safe(value):
    import base64
    import datetime
    import decimal
    import uuid

    if isinstance(value, (uuid.UUID, datetime.datetime, datetime.date, datetime.time)):
        return str(value)
    if isinstance(value, decimal.Decimal):
        # String, not float — NUMERIC must round-trip without precision loss.
        return str(value)
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    return value