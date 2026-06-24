"""
plugins.admin.guard
===================
``require_admin(ctx)`` — the single access gate for every admin route.

Relay's per-route ``roles=`` is declared-but-not-enforced (the auth/RBAC guard
in the ASGI pipeline is a Phase-4/5 stub). So admin does NOT rely on ``roles=``.
Instead, every handler calls ``require_admin(ctx)`` as its first line.

authn's ``before_req`` authenticator has already populated the request's
``UserContext`` from the Bearer token by the time a handler runs, so this guard
just reads it and checks the superuser flag. No DB hit.
"""

from __future__ import annotations

from arc.kernel.context import get_user

from plugins.admin.errors import AdminAuthError, ForbiddenError


def require_admin(ctx):
    """Return the current UserContext if it is an authenticated superuser;
    raise 401 if unauthenticated, 403 if authenticated but not a superuser."""
    uc = get_user()
    if uc is None or not getattr(uc, "id", None):
        raise AdminAuthError("Authentication required.")
    if not getattr(uc, "is_superuser", False):
        raise ForbiddenError("Superuser access required.")
    return uc


__all__ = ["require_admin"]
