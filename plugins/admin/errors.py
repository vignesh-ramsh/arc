"""
plugins.admin.errors
=====================
admin raises errors that subclass relay's ``RelayError`` so the existing ASGI
pipeline catches them and renders the standard ``{"error": {...}}`` envelope
with the right HTTP status — no changes to relay needed.

Importing the base from ``plugins.relay`` is the sanctioned pattern (it is
relay's public API, the same surface hrms/sales/authn already import).

    source = "admin"
    AdminAuthError  401  unauthorized   (no authenticated user)
    ForbiddenError  403  forbidden      (authenticated but not a superuser)
    AdminError      400  admin_error    (bad input to an admin operation)
"""

from __future__ import annotations

from plugins.relay import RelayError


class AdminAuthError(RelayError):
    source = "admin"
    status = 401
    code = "unauthorized"


class ForbiddenError(RelayError):
    source = "admin"
    status = 403
    code = "forbidden"


class AdminError(RelayError):
    source = "admin"
    status = 400
    code = "admin_error"


__all__ = ["AdminAuthError", "ForbiddenError", "AdminError"]
