"""
plugins.admin.routes.users
==========================
User management. Reads come straight from the ``AuthUser`` table via ``arc``;
writes go through the ``auth.context`` service (carried on ``admin_ctx``) so
password hashing, uniqueness checks and session revocation are never bypassed.

  GET  /api/v1/admin/users                          list users
  POST /api/v1/admin/users                          create  {username,email,password,is_superuser?,roles?}
  POST /api/v1/admin/users/{username}/enable        activate (idempotent)
  POST /api/v1/admin/users/{username}/disable       deactivate + revoke sessions
  POST /api/v1/admin/users/{username}/set-password  reset password  {password}
  GET  /api/v1/admin/users/{username}/sessions      active sessions
"""

from __future__ import annotations

from plugins.relay import get, post, arc, BadParam

from plugins.admin import admin_ctx
from plugins.admin.guard import require_admin

_USER_FIELDS = ["id", "username", "email", "is_active", "is_superuser",
                "roles", "created_at"]


def _body(ctx) -> dict:
    return ctx.data if isinstance(ctx.data, dict) else {}


@get("/api/v1/admin/users", table="AuthUser")
async def list_users(ctx):
    require_admin(ctx)
    rows = await arc.list(
        "AuthUser", fields=_USER_FIELDS, order="-created_at", limit=500)
    return {"users": rows}


@post("/api/v1/admin/users", table="AuthUser")
async def create_user(ctx):
    require_admin(ctx)
    d = _body(ctx)
    username = (d.get("username") or "").strip()
    email = (d.get("email") or "").strip()
    password = d.get("password") or ""
    if not username or not email or not password:
        raise BadParam("username, email and password are required.")
    # auth_service enforces strength, uniqueness and argon2id hashing; it raises
    # AuthError / ConflictError (relay errors) which the ASGI layer renders.
    res = await admin_ctx.auth.create_user(
        username, email, password,
        is_superuser=bool(d.get("is_superuser")),
        roles=list(d.get("roles") or []),
    )
    return res


@post("/api/v1/admin/users/{username}/enable", table="AuthUser")
async def enable_user(ctx):
    require_admin(ctx)
    changed = await admin_ctx.auth.set_active(ctx.params["username"], True)
    return {"username": ctx.params["username"], "is_active": True, "changed": changed}


@post("/api/v1/admin/users/{username}/disable", table="AuthUser")
async def disable_user(ctx):
    require_admin(ctx)
    changed = await admin_ctx.auth.set_active(ctx.params["username"], False)
    return {"username": ctx.params["username"], "is_active": False, "changed": changed}


@post("/api/v1/admin/users/{username}/set-password", table="AuthUser")
async def set_password(ctx):
    require_admin(ctx)
    password = (_body(ctx).get("password") or "")
    if not password:
        raise BadParam("password is required.")
    await admin_ctx.auth.set_password(ctx.params["username"], password)
    return {"username": ctx.params["username"], "password_reset": True}


@get("/api/v1/admin/users/{username}/sessions", table="AuthSession")
async def list_sessions(ctx):
    require_admin(ctx)
    user = await arc.get("AuthUser", {"username": ctx.params["username"]})
    if user is None:
        raise BadParam(f"No such user: {ctx.params['username']}")
    rows = await arc.list(
        "AuthSession",
        fields=["session_key", "ip", "agent", "last_access", "expires_at", "status"],
        filters=[("user_id", "eq", user["id"]), ("status", "eq", "active")],
        order="-created_at", limit=200,
    )
    return {"username": ctx.params["username"], "sessions": rows}
