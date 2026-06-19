"""
plugins.authn.routes.auth
=========================
Auth endpoints, auto-discovered by relay (it scans every plugin's routes/*.py).

  POST /api/v1/authn/login     {username|email, password}        → token pair
  POST /api/v1/authn/refresh   {refresh_token}                   → token pair
  POST /api/v1/authn/logout    {refresh_token}                   → {revoked}
  GET  /api/v1/authn/me                                          → current identity

login/refresh/logout are marked Guest (public): the before_req authenticator
only rejects a PRESENT-but-invalid token, so these work with no Authorization
header. /me requires a valid access token.

Handlers use the bound ``auth_service`` singleton — the same pattern by which
hrms/sales handlers use the ``arc`` singleton.
"""

from __future__ import annotations

from arc.kernel.context import get_user
from plugins.relay import post, get, BadParam

from plugins.authn import auth_service
from plugins.authn.errors import AuthError
from plugins.authn.guards import client_ip


def _body(ctx) -> dict:
    data = ctx.data if isinstance(ctx.data, dict) else {}
    return data


@post("/api/v1/authn/login", roles=["Guest"], table="AuthSession", rt_limit=10)
async def login(ctx):
    data = _body(ctx)
    identifier = (data.get("username") or data.get("email") or "").strip()
    password = data.get("password") or ""
    if not identifier or not password:
        raise BadParam("username (or email) and password are required.")
    ip = client_ip(ctx, trust_forwarded=auth_service.config.trust_forwarded)
    agent = ctx.request.headers.get("user-agent")
    return await auth_service.login(identifier, password, ip=ip, agent=agent)


@post("/api/v1/authn/refresh", roles=["Guest"], table="AuthSession", rt_limit=30)
async def refresh(ctx):
    data = _body(ctx)
    token = (data.get("refresh_token") or "").strip()
    if not token:
        raise BadParam("refresh_token is required.")
    ip = client_ip(ctx, trust_forwarded=auth_service.config.trust_forwarded)
    agent = ctx.request.headers.get("user-agent")
    return await auth_service.refresh(token, ip=ip, agent=agent)


@post("/api/v1/authn/logout", roles=["Guest"], table="AuthSession")
async def logout(ctx):
    data = _body(ctx)
    token = (data.get("refresh_token") or "").strip()
    if not token:
        raise BadParam("refresh_token is required.")
    revoked = await auth_service.logout(token)
    return {"revoked": revoked}


@get("/api/v1/authn/me", table="AuthUser")
async def me(ctx):
    uc = get_user()
    if uc is None or not uc.id:
        raise AuthError("Authentication required.")
    return {
        "id": uc.id,
        "email": uc.email,
        "roles": list(uc.roles),
        "is_superuser": uc.is_superuser,
    }
