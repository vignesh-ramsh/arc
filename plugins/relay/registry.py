"""
arc.plugins.relay.registry
==========================
The ``Relay`` registrar — the decorator surface plugins use to contribute
routes and document-event hooks. It imports NOTHING from another plugin.

Events split into two tiers:

  PRE-COMMIT (inside the write transaction — raising rolls everything back):
    validate          insert + update   field checks, cross-record invariants
    before_insert     insert            normalise / set computed defaults
    after_insert      insert            denormalised counters, child records
    before_update     update            guard state transitions, normalise
    after_update      update            sync derived fields
    before_delete     delete            block if dependents exist
    after_delete      delete            cascade soft-deletes, cleanup

  POST-COMMIT (after the transaction is committed — write is permanent):
    on_insert         insert            notifications, webhooks, search index
    on_update         update            notifications, webhooks, cache bust
    on_delete         delete            notifications, cleanup external systems

  Post-commit hooks fire as a Starlette BackgroundTask — the HTTP response
  is already sent to the client before they run. A failing post-commit hook
  is LOGGED ONLY; it never affects the response or undoes the write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable

Handler = Callable[..., Awaitable]
Hook = Callable[..., Awaitable]

# Pre-commit — failure rolls the transaction back.
PRE_COMMIT_EVENTS = frozenset(
    {
        "validate",
        "before_insert",
        "after_insert",
        "before_update",
        "after_update",
        "before_delete",
        "after_delete",
    }
)

# Post-commit — fire after the commit lands; failure is logged only.
POST_COMMIT_EVENTS = frozenset(
    {
        "on_insert",
        "on_update",
        "on_delete",
    }
)

EVENTS = PRE_COMMIT_EVENTS | POST_COMMIT_EVENTS

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ValidationError(Exception):
    """Raised by a pre-commit hook (via ``doc.fail()``) -> 422 + rollback."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


@dataclass(frozen=True)
class RouteSpec:
    path: str
    methods: tuple[str, ...]
    handler: Handler
    name: str
    source: str


class Relay:
    """A registrar instance. One is created by the relay plugin and shared."""

    def __init__(self) -> None:
        self._routes: list[RouteSpec] = []
        self._hooks: dict[tuple[str, str], list[Hook]] = {}

    # ── Route decorators ───────────────────────────────────────────────
    def route(
        self,
        path: str,
        *,
        methods: tuple[str, ...] | list[str] = ("GET",),
        name: str | None = None,
        source: str = "",
    ) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            self._routes.append(
                RouteSpec(
                    path=path,
                    methods=tuple(m.upper() for m in methods),
                    handler=handler,
                    name=name or handler.__name__,
                    source=source or getattr(handler, "__module__", ""),
                )
            )
            return handler
        return decorator

    def get(self, path: str, **kw) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("GET",), **kw)

    def post(self, path: str, **kw) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("POST",), **kw)

    def patch(self, path: str, **kw) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("PATCH",), **kw)

    def put(self, path: str, **kw) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("PUT",), **kw)

    def delete(self, path: str, **kw) -> Callable[[Handler], Handler]:
        return self.route(path, methods=("DELETE",), **kw)

    # ── Hook decorator ─────────────────────────────────────────────────
    def hook(self, table: str, event: str) -> Callable[[Hook], Hook]:
        """Register any hook — pre-commit or post-commit.

        Pre-commit:  @relay.hook("Employee", "validate")
        Post-commit: @relay.hook("Employee", "on_insert")
        """
        if event not in EVENTS:
            raise ValueError(
                f"Unknown relay event '{event}'.\n"
                f"  Pre-commit:  {sorted(PRE_COMMIT_EVENTS)}\n"
                f"  Post-commit: {sorted(POST_COMMIT_EVENTS)}"
            )
        if not _IDENT.match(table):
            raise ValueError(f"Invalid table name '{table}'.")

        def decorator(fn: Hook) -> Hook:
            self._hooks.setdefault((table, event), []).append(fn)
            return fn
        return decorator

    # ── Read accessors ─────────────────────────────────────────────────
    @property
    def routes(self) -> list[RouteSpec]:
        return list(self._routes)

    def hooks_for(self, table: str, event: str) -> list[Hook]:
        return self._hooks.get((table, event), [])

    def hook_summary(self) -> list[tuple[str, str, int]]:
        return sorted((t, e, len(h)) for (t, e), h in self._hooks.items())