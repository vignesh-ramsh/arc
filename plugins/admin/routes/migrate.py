"""
plugins.admin.routes.migrate
============================
The Migrate button.

  POST /api/v1/admin/migrate   body {confirm_destructive?: bool}  → run `arc db migrate`
  POST /api/v1/admin/migrate/plan                                 → dry-run `arc db plan`

Destructive operations (DROP COLUMN, DROP+ADD) require ``confirm_destructive``
just like the CLI's ``--confirm-destructive`` flag.
"""

from __future__ import annotations

from plugins.relay import post

from plugins.admin import admin_ctx, migrate
from plugins.admin.guard import require_admin


@post("/api/v1/admin/migrate")
async def trigger_migrate(ctx):
    require_admin(ctx)
    body = ctx.data if isinstance(ctx.data, dict) else {}
    confirm = bool(body.get("confirm_destructive"))
    result = await migrate.run_migrate(
        confirm_destructive=confirm,
        cwd=admin_ctx.project_root,
        command=admin_ctx.migrate_cmd,
    )
    return result


@post("/api/v1/admin/migrate/plan")
async def preview_migrate(ctx):
    require_admin(ctx)
    result = await migrate.run_plan(
        cwd=admin_ctx.project_root,
        command=admin_ctx.migrate_cmd,
    )
    return result
