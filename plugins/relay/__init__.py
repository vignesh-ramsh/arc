"""
plugins.relay
=============
Decorator routing + a context-bound document API (``arc``) with a pre/post-commit
hook pipeline, plus background-task and schedule registration. relay imports no
other plugin — it reaches the database through the ``db.session`` capability,
softly consumes redix (cache/queue/scheduler) when present, and exposes its own
services as capabilities and as the flat ``arc`` surface:

    relay.router      the registrar (route + hook + task + schedule decorators)
    relay.documents   the ``arc`` gateway (the single DB API)

Two registration styles, both feeding the SAME singleton:

1) Module-level decorators (least boilerplate — auto-discovered):

       from plugins.relay import get, post, patch, delete, stream, hook
       import arc   # the flat surface, assembled by the kernel at build time

       @post(route="/employees", roles=["Manager"], rt_limit=30)   # upsert
       async def create(ctx):
           return await arc.save("Employee", ctx.data)

       @hook("Employee", ["before_insert", "before_update"])
       async def stamp(doc):
           doc.require("employee_code")

       @task("send_welcome_email")
       async def send_welcome_email(user_id, email): ...

       @scheduled("nightly_cleanup")
       async def nightly_cleanup(): ...

2) Capability (explicit wiring in a plugin's contribute()):

       def contribute(self, rt):
           r = rt.capabilities.require("relay.router")
           @r.get("/ping")
           async def ping(ctx): return {"ok": True}

The ``arc`` write surface: save (upsert), update (update-only), save_many
(per-row upserts, atomic by default), update_many (bulk update-by-filter), rm /
rm_many (soft delete). The cache/queue/scheduler facade (``arc.get_cache`` /
``arc.enqueue`` / ``arc.schedule_cron`` …) and the streaming bulk variants
(``arc.import_streamed`` …) are contributed to the flat ``arc`` surface too; use
them via ``import arc`` inside handlers/hooks/tasks.
"""

from __future__ import annotations

from plugins.relay.documents import Arc, Document, TxContext
from plugins.relay.errors import (
    AmbiguousTarget, BadJSON, BadParam, ConflictError, DataError, HookAbort,
    HookError, IntegrityError, NotFoundError, PayloadTooLarge, RelayError,
    RequestError, ValidationError,
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
patch = relay.patch
delete = relay.delete
stream = relay.stream

# Hook decorators
hook = relay.hook
on_commit = relay.on_commit
on_rollback = relay.on_rollback
before_req = relay.before_req
after_req = relay.after_req

# Task + schedule decorators (handler declarations; triggered via arc.*)
task = relay.task
scheduled = relay.scheduled

__all__ = [
    # singletons
    "relay", "arc",
    # decorators
    "get", "post", "patch", "delete", "stream",
    "hook", "on_commit", "on_rollback", "before_req", "after_req",
    "task", "scheduled",
    # types
    "Relay", "RouteSpec", "RateLimit", "Arc", "Document", "TxContext",
    # errors
    "RelayError", "HookError", "ValidationError", "HookAbort",
    "DataError", "NotFoundError", "ConflictError", "IntegrityError", "AmbiguousTarget",
    "RequestError", "BadJSON", "BadParam", "PayloadTooLarge",
]