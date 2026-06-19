"""
plugins.authn.errors
=====================
authn raises errors that subclass relay's ``RelayError`` so the existing ASGI
pipeline catches them and renders the same ``{"error": {...}}`` envelope with
the right HTTP status — no changes to relay needed.

    source = "auth"
    AuthError       401  unauthorized   (no/invalid/expired credentials or token)
    ForbiddenError  403  forbidden      (authenticated but lacks the role)
    LockedError     423  account_locked (too many failed logins)

Importing the base from ``plugins.relay`` is the sanctioned pattern — it is
relay's public API (re-exported in ``plugins.relay.__all__``), the same surface
hrms/sales handlers already import ``arc``/``get``/``post`` from.
"""

from __future__ import annotations

from plugins.relay import RelayError


class AuthError(RelayError):
    source = "auth"
    status = 401
    code = "unauthorized"


class ForbiddenError(RelayError):
    source = "auth"
    status = 403
    code = "forbidden"


class LockedError(RelayError):
    source = "auth"
    status = 423
    code = "account_locked"
