"""
arc.kernel.context
==================
Per-request context carried through ``contextvars`` so any plugin (db audit
columns, api handlers, permission checks) can read the current request and
user without threading them through call signatures.

Single-tenant: there is no TenantContext.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class RequestContext:
    id: str
    method: str = ""
    path: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class UserContext:
    id: str = ""
    email: str = ""
    roles: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    is_superuser: bool = False


_request: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "arc_request", default=None
)
_user: contextvars.ContextVar[UserContext | None] = contextvars.ContextVar(
    "arc_user", default=None
)


def set_request(ctx: RequestContext | None) -> None:
    _request.set(ctx)


def get_request() -> RequestContext | None:
    return _request.get()


def set_user(ctx: UserContext | None) -> None:
    _user.set(ctx)


def get_user() -> UserContext | None:
    return _user.get()


def clear() -> None:
    _request.set(None)
    _user.set(None)
