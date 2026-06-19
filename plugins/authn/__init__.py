from __future__ import annotations

from plugins.authn.service import AuthService

# Process-wide singleton — bound in AuthnPlugin.setup().
auth_service = AuthService()

__all__ = ["auth_service", "AuthService"]