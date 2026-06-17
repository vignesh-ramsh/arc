"""
plugins.relay
=============
Decorator routing + a context-bound document API (``arc``) with a pre/post-commit
hook pipeline. relay imports no other plugin — it reaches the database only
through the ``db.session`` capability and exposes its own services as
capabilities:

    relay.router      the registrar (route + hook decorators)
    relay.documents   the ``arc`` gateway (the single DB API)

Two registration styles, both feeding the SAME singleton:

1) Module-level decorators (least boilerplate — auto-discovered):

       from plugins.relay import get, post, delete, stream, hook, arc

       @post(route="/employees", roles=["Manager"], rt_limit=30)
       async def create(ctx):
           return await arc.save("Employee", ctx.data)

       @hook("Employee", ["before_insert", "before_update"])
       async def stamp(doc):
           doc.require("employee_code")

2) Capability (explicit wiring in a plugin's contribute()):

       def contribute(self, rt):
           r = rt.capabilities.require("relay.router")
           @r.get("/ping")
           async def ping(ctx): return {"ok": True}
"""

from __future__ import annotations

from plugins.relay.documents import Arc, Document, TxContext
from plugins.relay.errors import (
    AmbiguousTarget, BadJSON, BadParam, ConflictError, DataError, HookAbort,
    HookError, IntegrityError, NotFoundError, RelayError, RequestError,
    ValidationError,
)
from plugins.relay.registry import RateLimit, Relay, RouteSpec

# Process-wide singletons. The relay plugin binds `arc` to db.session at setup()
# and shares THIS `relay` registrar so module-level decorators and the
# capability registrar are the same object.
relay = Relay()
arc = Arc()

# Route decorators
get = relay.get
post = relay.post
delete = relay.delete
stream = relay.stream

# Hook decorators
hook = relay.hook
on_commit = relay.on_commit
on_rollback = relay.on_rollback
before_req = relay.before_req
after_req = relay.after_req

__all__ = [
    # singletons
    "relay", "arc",
    # decorators
    "get", "post", "delete", "stream",
    "hook", "on_commit", "on_rollback", "before_req", "after_req",
    # types
    "Relay", "RouteSpec", "RateLimit", "Arc", "Document", "TxContext",
    # errors
    "RelayError", "HookError", "ValidationError", "HookAbort",
    "DataError", "NotFoundError", "ConflictError", "IntegrityError", "AmbiguousTarget",
    "RequestError", "BadJSON", "BadParam",
]