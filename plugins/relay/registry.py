"""
plugins.relay.registry
=======================
The ``Relay`` registrar — the decorator surface plugins use to contribute
routes, hooks, background tasks, and schedules. It imports nothing from another
plugin.

Route decorators (verbs: get / post / patch / delete / stream):

    @post(route="/employees", roles=["Manager"], rt_limit=30)   # create-or-update (upsert)
    async def create(ctx): ...

    @patch(route="/employees/{id}", roles=["Manager"])          # update-existing-only
    async def edit(ctx): ...

Document hooks (one fn, many events; single ``doc`` arg):

    @hook("Employee", ["after_insert", "after_update"])
    async def touch(doc): ...

Background tasks + schedules (handler declarations; triggered via arc.*):

    @task("send_welcome_email")
    async def send_welcome_email(user_id, email): ...

    @scheduled("nightly_cleanup")
    async def nightly_cleanup(): ...

``@task`` / ``@scheduled`` only REGISTER the handler by name. The trigger side is
``arc.enqueue(...)`` / ``arc.schedule_cron(...)`` (relay's ARC_SURFACE facade),
which talks to redix's queue.client / scheduler.client — or a fallback when redix
is absent. The redix queue/scheduler WORKERS resolve handlers by name through
``task_handler`` / ``scheduled_handler`` here; they never import business code.

Event tiers
-----------
PRE-COMMIT (per doc, inside the write txn — raising rolls everything back):
    validate, before_insert, after_insert,
    before_update, after_update, before_delete, after_delete
POST-COMMIT per doc (background, after the response):
    on_change                      (fires on insert / update / delete)
POST-COMMIT per transaction (GLOBAL — not table-keyed; background):
    on_commit(tx), on_rollback(tx)

Global request hooks (GLOBAL):
    before_req(ctx)  — may raise / return a response to short-circuit
    after_req(ctx, response) — always runs, even on error

Rate limit
----------
``rt_limit`` is an int request count. Period and scope are fixed (per minute,
per user). ``rt_limit=None`` means "inherit the configured default" — relay
does NOT bake the default in at decoration time (kept compile-ahead: the
default is resolved from plugin.toml by the enforcing middleware).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from arc.kernel.logger import get_logger
from plugins.relay.errors import ValidationError  # re-exported for back-compat

log = get_logger("arc.plugin.relay.registry")

Handler = Callable[..., Awaitable]
Hook = Callable[..., Awaitable]
Task = Callable[..., object]

# ── Event taxonomy ───────────────────────────────────────────────────────────

PRE_COMMIT_EVENTS = frozenset({
    "validate",
    "before_insert", "after_insert",
    "before_update", "after_update",
    "before_delete", "after_delete",
})
POST_COMMIT_DOC_EVENTS = frozenset({"on_change"})
POST_COMMIT_TX_EVENTS = frozenset({"on_commit", "on_rollback"})

# Events bindable via @hook(table, ...) — table-scoped, per document.
DOC_EVENTS = PRE_COMMIT_EVENTS | POST_COMMIT_DOC_EVENTS

# Per-write skippable hooks (the skip_* flags on save/update/save_many/rm/...).
SKIPPABLE_EVENTS = PRE_COMMIT_EVENTS | POST_COMMIT_DOC_EVENTS

# PATCH added: update-existing-only. POST stays upsert. (See documents.Arc.)
VERBS = frozenset({"GET", "POST", "PATCH", "DELETE", "STREAM"})
_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
GUEST_ROLE = "Guest"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── Declarative route metadata ───────────────────────────────────────────────

@dataclass(frozen=True)
class RateLimit:
    """Period and scope are fixed for v1; only the count varies."""
    count: int
    period: str = "minute"
    scope: str = "user"


@dataclass(frozen=True)
class RouteSpec:
    path: str
    methods: tuple[str, ...]
    handler: Handler
    name: str
    source: str
    roles: tuple[str, ...] = ()
    rate_limit: RateLimit | None = None      # None → inherit configured default
    cache: object | None = None              # route-cache spec (enforced via arc cache facade)
    stream: bool = False
    table: str | None = None                 # the table this route operates on (for `arc relay` -t)

    @property
    def is_write(self) -> bool:
        return any(m in _WRITE_METHODS for m in self.methods)

    @property
    def is_guest(self) -> bool:
        return GUEST_ROLE in self.roles


def _norm_roles(roles) -> tuple[str, ...]:
    if not roles:
        return ()
    roles = tuple(roles)
    if GUEST_ROLE in roles and len(roles) > 1:
        log.warning(
            "arc.relay.guest_with_roles",
            detail="roles= mixes 'Guest' with real roles; Guest makes the route "
                   "public, the others have no effect",
            roles=roles,
        )
    return roles


def _norm_rt_limit(rt_limit) -> RateLimit | None:
    if rt_limit is None:
        return None
    if isinstance(rt_limit, RateLimit):
        return rt_limit
    if isinstance(rt_limit, int) and rt_limit > 0:
        return RateLimit(count=rt_limit)
    raise ValueError(f"rt_limit must be a positive int or None, got {rt_limit!r}")


# ── Registrar ────────────────────────────────────────────────────────────────

class Relay:
    """One registrar instance, created by the relay plugin and shared with the
    module-level decorator proxies."""

    def __init__(self) -> None:
        self._routes: list[RouteSpec] = []
        self._hooks: dict[tuple[str, str], list[Hook]] = {}
        self._tx_hooks: dict[str, list[Hook]] = {"on_commit": [], "on_rollback": []}
        self._req_hooks: dict[str, list[Hook]] = {"before_req": [], "after_req": []}
        self._tasks: dict[str, Task] = {}        # @task name -> handler
        self._scheduled: dict[str, Task] = {}     # @scheduled name -> handler

    # ── route decorators ────────────────────────────────────────────────
    def _add_route(self, methods, route, *, roles, rt_limit, cache, stream,
                   name, source, table=None) -> Callable[[Handler], Handler]:
        if not route.startswith("/"):
            raise ValueError(f"route must start with '/': {route!r}")

        def decorator(handler: Handler) -> Handler:
            self._routes.append(RouteSpec(
                path=route,
                methods=tuple(methods),
                handler=handler,
                name=name or handler.__name__,
                source=source or getattr(handler, "__module__", ""),
                roles=_norm_roles(roles),
                rate_limit=_norm_rt_limit(rt_limit),
                cache=cache,
                stream=stream,
                table=table,
            ))
            return handler
        return decorator

    def get(self, route: str, *, roles=(), rt_limit=None, cache=None,
            name=None, source="", table=None) -> Callable[[Handler], Handler]:
        return self._add_route(("GET",), route, roles=roles, rt_limit=rt_limit,
                               cache=cache, stream=False, name=name, source=source,
                               table=table)

    def post(self, route: str, *, roles=(), rt_limit=None, cache=None,
             name=None, source="", table=None) -> Callable[[Handler], Handler]:
        return self._add_route(("POST",), route, roles=roles, rt_limit=rt_limit,
                               cache=cache, stream=False, name=name, source=source,
                               table=table)

    def patch(self, route: str, *, roles=(), rt_limit=None, cache=None,
              name=None, source="", table=None) -> Callable[[Handler], Handler]:
        # Update-existing-only routes. POST upserts; PATCH never inserts.
        return self._add_route(("PATCH",), route, roles=roles, rt_limit=rt_limit,
                               cache=cache, stream=False, name=name, source=source,
                               table=table)

    def delete(self, route: str, *, roles=(), rt_limit=None, cache=None,
               name=None, source="", table=None) -> Callable[[Handler], Handler]:
        return self._add_route(("DELETE",), route, roles=roles, rt_limit=rt_limit,
                               cache=cache, stream=False, name=name, source=source,
                               table=table)

    def stream(self, route: str, *, roles=(), rt_limit=None, cache=None,
               name=None, source="", table=None) -> Callable[[Handler], Handler]:
        # Streaming reads are GET under the hood; stream=True changes the response wrap.
        return self._add_route(("GET",), route, roles=roles, rt_limit=rt_limit,
                               cache=cache, stream=True, name=name, source=source,
                               table=table)

    # ── document hooks ──────────────────────────────────────────────────
    def hook(self, table: str, events: str | list[str] | tuple[str, ...]
             ) -> Callable[[Hook], Hook]:
        """Bind one function to one or more table-scoped document events."""
        if not _IDENT.match(table):
            raise ValueError(f"Invalid table name {table!r}.")
        names = (events,) if isinstance(events, str) else tuple(events)
        if not names:
            raise ValueError("hook() needs at least one event.")
        for ev in names:
            if ev not in DOC_EVENTS:
                raise ValueError(
                    f"Unknown document event {ev!r}.\n"
                    f"  pre-commit:  {sorted(PRE_COMMIT_EVENTS)}\n"
                    f"  post-commit: {sorted(POST_COMMIT_DOC_EVENTS)}\n"
                    f"  (on_commit / on_rollback are global — use @on_commit / @on_rollback)"
                )

        def decorator(fn: Hook) -> Hook:
            for ev in names:
                bucket = self._hooks.setdefault((table, ev), [])
                if fn in bucket:
                    log.warning("arc.relay.duplicate_hook", table=table, event=ev,
                                hook=getattr(fn, "__name__", "?"))
                    continue
                bucket.append(fn)
            return fn
        return decorator

    # ── global transaction hooks ────────────────────────────────────────
    def on_commit(self, fn: Hook) -> Hook:
        self._tx_hooks["on_commit"].append(fn)
        return fn

    def on_rollback(self, fn: Hook) -> Hook:
        self._tx_hooks["on_rollback"].append(fn)
        return fn

    # ── global request hooks ────────────────────────────────────────────
    def before_req(self, fn: Hook) -> Hook:
        self._req_hooks["before_req"].append(fn)
        return fn

    def after_req(self, fn: Hook) -> Hook:
        self._req_hooks["after_req"].append(fn)
        return fn

    # ── background task + schedule registration ─────────────────────────
    def task(self, name: str) -> Callable[[Task], Task]:
        """Register a background task handler by name.

        Triggered via ``arc.enqueue(name, **kwargs)``. The redix queue worker
        resolves the handler with ``task_handler(name)`` and calls it with the
        enqueued kwargs. With redix absent, ``arc.enqueue`` runs it inline via a
        Starlette BackgroundTask.
        """
        if not name or not isinstance(name, str):
            raise ValueError("task() needs a non-empty string name.")

        def decorator(fn: Task) -> Task:
            if name in self._tasks and self._tasks[name] is not fn:
                log.warning("arc.relay.duplicate_task", task=name)
            self._tasks[name] = fn
            return fn
        return decorator

    def scheduled(self, name: str) -> Callable[[Task], Task]:
        """Register a scheduled job handler by name.

        Timing is declared separately via ``arc.schedule_cron(name, expr)`` /
        ``arc.schedule_every(name, ...)``. The redix scheduler worker dispatches
        the job onto the queue, which resolves the handler by name (a scheduled
        handler is also registered as a task so the queue worker can run it)."""
        if not name or not isinstance(name, str):
            raise ValueError("scheduled() needs a non-empty string name.")

        def decorator(fn: Task) -> Task:
            if name in self._scheduled and self._scheduled[name] is not fn:
                log.warning("arc.relay.duplicate_scheduled", schedule=name)
            self._scheduled[name] = fn
            # Also expose it as a task so the queue worker (which scheduler
            # dispatches through) can resolve it by name.
            self._tasks.setdefault(name, fn)
            return fn
        return decorator

    def task_handler(self, name: str) -> Task | None:
        return self._tasks.get(name)

    def scheduled_handler(self, name: str) -> Task | None:
        return self._scheduled.get(name)

    def task_names(self) -> list[str]:
        return sorted(self._tasks)

    def scheduled_names(self) -> list[str]:
        return sorted(self._scheduled)

    # ── read accessors ──────────────────────────────────────────────────
    @property
    def routes(self) -> list[RouteSpec]:
        return list(self._routes)

    def hooks_for(self, table: str, event: str) -> list[Hook]:
        return self._hooks.get((table, event), [])

    def has_any_hooks(self, table: str, events) -> bool:
        """True if any of *events* has at least one hook for *table*. Used by the
        batch write paths to choose a fast no-hook route."""
        return any(self._hooks.get((table, ev)) for ev in events)

    def tx_hooks(self, event: str) -> list[Hook]:
        return list(self._tx_hooks.get(event, []))

    def req_hooks(self, phase: str) -> list[Hook]:
        return list(self._req_hooks.get(phase, []))

    def hook_summary(self) -> list[tuple[str, str, int]]:
        return sorted((t, e, len(h)) for (t, e), h in self._hooks.items())

    def hook_items(self) -> list[tuple[str, str, list]]:
        """(table, event, [hook fns]) for every binding — lets callers resolve
        each hook's owning plugin from ``fn.__module__`` (used by `arc relay`)."""
        return sorted(((t, e, list(h)) for (t, e), h in self._hooks.items()),
                      key=lambda x: (x[0], x[1]))


__all__ = [
    "Relay", "RouteSpec", "RateLimit",
    "PRE_COMMIT_EVENTS", "POST_COMMIT_DOC_EVENTS", "POST_COMMIT_TX_EVENTS",
    "DOC_EVENTS", "SKIPPABLE_EVENTS", "VERBS", "GUEST_ROLE",
    "ValidationError", "Handler", "Hook", "Task",
]