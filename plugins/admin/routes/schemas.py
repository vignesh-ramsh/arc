"""
plugins.admin.routes.schemas
============================
Schema Viewer + Table Builder backend.

  GET  /api/v1/admin/schemas             tree of {plugin: [tables]} from _field_registry
  GET  /api/v1/admin/schemas/{table}     field rows for one table (404 if unknown)
  POST /api/v1/admin/schemas/{table}     validate + write plugins/<plugin>/schemas/<Table>.json

Writing a schema NEVER touches the database — it only produces the JSON file.
Apply it with the separate Migrate button (POST /api/v1/admin/migrate).
"""

from __future__ import annotations

from plugins.relay import get, post, BadParam, NotFoundError

from plugins.admin import admin_ctx
from plugins.admin import introspect, schema_io
from plugins.admin.guard import require_admin


@get("/api/v1/admin/schemas", table="_field_registry")
async def list_schemas(ctx):
    require_admin(ctx)
    tables = await introspect.list_tables()
    grouped: dict[str, list] = {}
    for t in tables:
        grouped.setdefault(t["plugin"], []).append(
            {"table": t["table_name"], "field_count": t["field_count"]})
    return {"plugins": grouped}


@get("/api/v1/admin/schemas/{table}", table="_field_registry")
async def get_schema(ctx):
    require_admin(ctx)
    table = ctx.params["table"]
    fields = await introspect.table_fields(table)
    if not fields:
        raise NotFoundError(f"No registered schema for table {table!r}.")
    plugin = fields[0]["plugin"]
    return {"table": table, "plugin": plugin, "fields": fields}


@post("/api/v1/admin/schemas/{table}")
async def write_schema(ctx):
    require_admin(ctx)
    table = ctx.params["table"]
    body = ctx.data if isinstance(ctx.data, dict) else {}
    plugin = (body.get("plugin") or "").strip()
    fields = body.get("fields")
    if not plugin:
        raise BadParam("plugin is required.")
    if not isinstance(fields, list):
        raise BadParam("fields must be a list.")
    # schema_io.validate raises AdminError (400) on any structural problem.
    result = schema_io.write_schema(
        table=table, plugin=plugin, fields=fields,
        project_root=admin_ctx.project_root,
    )
    return {"written": True, **result}
