"""
arc.plugins.api.crud
==================
Builds Starlette routes for a ``Resource``. Handlers obtain a session through
the ``db.session`` capability (a context-manager callable) rather than
importing the db plugin — api depends on the *capability*, not the module.

Skeleton coverage: list (paginated), get-by-id, create. The v1 feature set
(PATCH with optimistic locking, soft delete to _trash, 14 filter operators,
ETag/304, cursor pagination, field-level permissions) plugs in at the marked
seams without changing the route signatures.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import text

from arc.plugins.api.resource import Resource


def build_routes(resource: Resource, session_cm: Callable):
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    cols = ", ".join(f'"{c}"' for c in resource.read_columns)
    table = f'"{resource.table}"'

    def _serialize(row) -> dict:
        return {k: _json_safe(v) for k, v in row._mapping.items()}

    async def list_view(request: Request) -> JSONResponse:
        limit = min(
            int(request.query_params.get("limit", resource.page_size)),
            resource.max_page_size,
        )
        offset = int(request.query_params.get("offset", 0))
        # SEAM: filter compiler (?field=op:value) builds the WHERE clause here.
        sql = text(
            f'SELECT {cols} FROM {table} '
            f"ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
        )
        async with session_cm() as session:
            result = await session.execute(sql, {"limit": limit, "offset": offset})
            rows = [_serialize(r) for r in result]
        return JSONResponse({"data": rows, "limit": limit, "offset": offset})

    async def get_view(request: Request) -> JSONResponse:
        rid = request.path_params["id"]
        sql = text(f'SELECT {cols} FROM {table} WHERE id = :id')
        async with session_cm() as session:
            row = (await session.execute(sql, {"id": rid})).first()
        if row is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse({"data": _serialize(row)})

    async def create_view(request: Request) -> JSONResponse:
        body = await request.json()
        payload = {k: body.get(k) for k in resource.writable if k in body}
        if not payload:
            return JSONResponse({"error": "no_writable_fields"}, status_code=422)
        names = ", ".join(f'"{k}"' for k in payload)
        binds = ", ".join(f":{k}" for k in payload)
        sql = text(
            f'INSERT INTO {table} ({names}) VALUES ({binds}) RETURNING {cols}'
        )
        async with session_cm() as session:
            row = (await session.execute(sql, payload)).first()
        return JSONResponse({"data": _serialize(row)}, status_code=201)

    return [
        Route(resource.path, list_view, methods=["GET"]),
        Route(resource.path, create_view, methods=["POST"]),
        Route(resource.path + "/{id}", get_view, methods=["GET"]),
    ]


def _json_safe(value):
    import datetime
    import uuid

    if isinstance(value, (uuid.UUID, datetime.datetime, datetime.date)):
        return str(value)
    return value
