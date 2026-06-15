"""
plugins.relay
================
Decorator-based routing plus a document-event pipeline that enforces data
integrity. relay imports no other plugin — it reaches the database through the
``db.session`` capability and exposes its own services as capabilities:

    relay.router      the registrar (route + hook decorators)
    relay.documents   the DocumentGateway (integrity-checked writes)

Two registration styles:

1) Capability (zero imports — recommended):

       def contribute(self, rt):
           relay = rt.capabilities.require("relay.router")
           @relay.get("/ping")
           async def ping(ctx): return {"ok": True}

2) Module-level decorators (the one optional registration import). These proxy
   to the SAME singleton the plugin provides, so both styles interoperate:

       from plugins.relay import route, get, post, patch, delete, hook

Public types for handlers/hooks: ``Context``, ``Document``, ``ValidationError``.
"""

from __future__ import annotations

from plugins.relay.documents import (
    ConflictError,
    Document,
    DocumentGateway,
    NotFoundError,
)
from plugins.relay.registry import EVENTS, Relay, RouteSpec, ValidationError

# Process-wide singleton backing the module-level decorators. The relay plugin
# constructs its OWN Relay() and provides that as relay.router; for the
# module-level proxies to feed the same instance the plugin imports this one.
relay = Relay()

route = relay.route
get = relay.get
post = relay.post
put = relay.put
patch = relay.patch
delete = relay.delete
hook = relay.hook

__all__ = [
    "relay",
    "route",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "hook",
    "Relay",
    "RouteSpec",
    "EVENTS",
    "Document",
    "DocumentGateway",
    "ValidationError",
    "ConflictError",
    "NotFoundError",
]