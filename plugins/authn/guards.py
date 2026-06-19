"""
plugins.authn.guards
=====================
Phase-4 enforcement primitives.

Route-spec ``roles=`` enforcement is a Phase-5 concern that belongs inside
relay's request pipeline (the asgi STUB that reads ``spec.roles``). Until that
lands, ``require_roles`` gives handlers — including authn's own admin routes —
an explicit, composable guard. Superusers bypass every check.

    from plugins.authn.guards import require_roles

    @post("/api/v1/authn/users", table="AuthUser")
    @require_roles("Admin")
    async def create_user(ctx): ...
"""

from __future__ import annotations

import functools

from arc.kernel.context import get_user

from plugins.authn.errors import AuthError, ForbiddenError


def current_user():
    """The UserContext set by the before_req authenticator, or None."""
    return get_user()


def require_roles(*roles: str):
    """Require an authenticated user holding at least one of *roles*. No roles
    listed → just require authentication. Superusers always pass."""
    needed = set(roles)

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapped(ctx):
            uc = get_user()
            if uc is None or not uc.id:
                raise AuthError("Authentication required.")
            if uc.is_superuser:
                return await handler(ctx)
            if needed and not (needed & set(uc.roles)):
                raise ForbiddenError("You do not have permission to perform this action.")
            return await handler(ctx)
        return wrapped
    return decorator


def client_ip(ctx, *, trust_forwarded: bool) -> str | None:
    """Best-effort client IP. With trust_forwarded, take the first hop of
    X-Forwarded-For (only safe behind a trusted proxy); otherwise the socket peer."""
    if trust_forwarded:
        xff = ctx.request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    client = getattr(ctx.request, "client", None)
    return getattr(client, "host", None) if client else None
