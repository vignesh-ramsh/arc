"""
plugins.relay.registry
=======================
The ``Relay`` registrar — the decorator surface plugins use to contribute
routes and hooks. It imports nothing from another plugin.

Route decorators (verbs: get / post / delete / stream only):

    @post(route="/employees", roles=["Manager"], rt_limit=30)
    async def create(ctx): ...

Document hooks (one fn, many events; single ``doc`` arg):

    @hook("Employee", ["after_insert", "after_update"])
    async def touch(doc): ...

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
from dataclasses import dataclass, field as dc_field
from typing import Awaitable, Callable

from arc.kernel.logger import get_logger
from plugins.relay.errors import ValidationError  # re-exported for back-compat

log = get_logger("arc.plugin.relay.registry")

Handler = Callable[..., Awaitable]
Hook = Callable[..., Awaitable]

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

# Per-write skippable hooks (the skip_* flags on save/rm/rm_many).
SKIPPABLE_EVENTS = PRE_COMMIT_EVENTS | POST_COMMIT_DOC_EVENTS

VERBS = frozenset({"GET", "POST", "DELETE", "STREAM"})
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
    cache: object | None = None              # STUB — accepted, not yet enforced
    stream: bool = False

    @property
    def is_write(self) -> bool:
        return any(m in ("POST", "DELETE") for m in self.methods)

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

    # ── route decorators ────────────────────────────────────────────────
    def _add_route(self, methods, route, *, roles, rt_limit, cache, stream,
                   name, source) -> Callable[[Handler], Handler]:
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
            ))
            return handler
        return decorator

    def get(self, route: str, *, roles=(), rt_limit=None, cache=None,
            name=None, source="") -> Callable[[Handler], Handler]:
        return self._add_route(("GET",), route, roles=roles, rt_limit=rt_limit,
                               cache=cache, stream=False, name=name, source=source)

    def post(self, route: str, *, roles=(), rt_limit=None, cache=None,
             name=None, source="") -> Callable[[Handler], Handler]:
        return self._add_route(("POST",), route, roles=roles, rt_limit=rt_limit,
                               cache=cache, stream=False, name=name, source=source)

    def delete(self, route: str, *, roles=(), rt_limit=None, cache=None,
               name=None, source="") -> Callable[[Handler], Handler]:
        return self._add_route(("DELETE",), route, roles=roles, rt_limit=rt_limit,
                               cache=cache, stream=False, name=name, source=source)

    def stream(self, route: str, *, roles=(), rt_limit=None, cache=None,
               name=None, source="") -> Callable[[Handler], Handler]:
        # Streaming reads are GET under the hood; stream=True changes the response wrap.
        return self._add_route(("GET",), route, roles=roles, rt_limit=rt_limit,
                               cache=cache, stream=True, name=name, source=source)

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

    # ── read accessors ──────────────────────────────────────────────────
    @property
    def routes(self) -> list[RouteSpec]:
        return list(self._routes)

    def hooks_for(self, table: str, event: str) -> list[Hook]:
        return self._hooks.get((table, event), [])

    def tx_hooks(self, event: str) -> list[Hook]:
        return list(self._tx_hooks.get(event, []))

    def req_hooks(self, phase: str) -> list[Hook]:
        return list(self._req_hooks.get(phase, []))

    def hook_summary(self) -> list[tuple[str, str, int]]:
        return sorted((t, e, len(h)) for (t, e), h in self._hooks.items())


__all__ = [
    "Relay", "RouteSpec", "RateLimit",
    "PRE_COMMIT_EVENTS", "POST_COMMIT_DOC_EVENTS", "POST_COMMIT_TX_EVENTS",
    "DOC_EVENTS", "SKIPPABLE_EVENTS", "VERBS", "GUEST_ROLE",
    "ValidationError", "Handler", "Hook",
]