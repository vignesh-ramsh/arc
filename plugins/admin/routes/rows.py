"""
plugins.admin.routes.rows
=========================
Generic row CRUD over any user table, for the Row Editor. Everything goes
through the ``arc`` document gateway, so hooks, soft-delete (_state=99),
system-field stripping and cursor pagination all apply.

  GET    /api/v1/admin/rows/{table}        list (cursor paginated)
  POST   /api/v1/admin/rows/{table}        create (arc.save → insert)
  PATCH  /api/v1/admin/rows/{table}/{id}   update-existing-only (arc.update)
  DELETE /api/v1/admin/rows/{table}/{id}   soft delete (arc.rm)

Auth and system tables are blocked from raw editing here — editing AuthUser rows
directly would bypass password hashing, so those go through the Users panel.
The block list is configurable via [plugins.admin] row_blocklist.
"""

from __future__ import annotations

import re

from plugins.relay import get, post, patch, delete, arc, BadParam

from plugins.admin import admin_ctx
from plugins.admin.guard import require_admin

_TABLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_LIST_HARD_CAP = 200


def _table(ctx) -> str:
    table = ctx.params["table"]
    if not _TABLE_RE.match(table):
        raise BadParam(f"invalid table name: {table!r}")
    if table.startswith("_") or table in admin_ctx.row_blocklist:
        raise BadParam(
            f"{table} is not editable from the Row Editor "
            f"(system/auth table — use the dedicated panel).")
    return table


@get("/api/v1/admin/rows/{table}")
async def list_rows(ctx):
    require_admin(ctx)
    table = _table(ctx)
    try:
        limit = int(ctx.query.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, _LIST_HARD_CAP))
    cursor = ctx.query.get("cursor") or None
    q = ctx.query.get("q") or None
    page = await arc.list_page(table, limit=limit, cursor=cursor)
    return page


@post("/api/v1/admin/rows/{table}")
async def create_row(ctx):
    require_admin(ctx)
    table = _table(ctx)
    body = ctx.data if isinstance(ctx.data, dict) else {}
    if not body:
        raise BadParam("request body must be a non-empty JSON object.")
    row = await arc.save(table, body)
    return row


@patch("/api/v1/admin/rows/{table}/{id}")
async def update_row(ctx):
    require_admin(ctx)
    table = _table(ctx)
    rid = ctx.params["id"]
    body = ctx.data if isinstance(ctx.data, dict) else {}
    if not body:
        raise BadParam("request body must be a non-empty JSON object.")
    row = await arc.update(table, {"id": rid}, body)
    return row


@delete("/api/v1/admin/rows/{table}/{id}")
async def delete_row(ctx):
    require_admin(ctx)
    table = _table(ctx)
    rid = ctx.params["id"]
    await arc.rm(table, {"id": rid})
    return {"table": table, "id": rid, "deleted": True}
