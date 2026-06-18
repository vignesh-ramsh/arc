"""
plugins.relay.errors
=====================
Source-segregated error families. Every relay error knows its HTTP ``status``,
a stable machine ``code``, and the ``source`` layer that produced it, so a 422
raised by a business hook is distinguishable from a 422 raised by the database.

    RelayError                       base — carries status / code / source / field
    ├── HookError      source="hook"      business / validation layer
    │   ├── ValidationError   422  validation_failed   (doc.fail / doc.require)
    │   └── HookAbort         4xx  hook_abort          (hook chooses the status)
    ├── DataError      source="db"        persistence layer
    │   ├── NotFoundError     404  not_found
    │   ├── ConflictError     409  conflict            (unique / FK)
    │   ├── IntegrityError    422  integrity           (check / not-null / type)
    │   └── AmbiguousTarget   409  ambiguous_target    (write filter matched >1 row)
    └── RequestError   source="request"   transport layer
        ├── BadJSON           400  invalid_json
        ├── BadParam          400  invalid_param
        └── PayloadTooLarge   413  payload_too_large   (body / bulk-row cap)

The ASGI layer maps any RelayError to ``{"error": {...}}`` via ``to_dict()``;
anything that is not a RelayError becomes a generic 500.
"""

from __future__ import annotations

from typing import Any


class RelayError(Exception):
    """Base for every error relay raises. Subclasses set status/code/source."""

    status: int = 500
    code: str = "internal_error"
    source: str = "relay"

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "source": self.source,
            "code": self.code,
            "message": self.message,
            "status": self.status,
        }
        if self.field is not None:
            body["field"] = self.field
        return {"error": body}


# ── Hook / business layer ────────────────────────────────────────────────────

class HookError(RelayError):
    source = "hook"


class ValidationError(HookError):
    """Raised by a pre-commit hook via ``doc.fail()`` / ``doc.require()``."""

    status = 422
    code = "validation_failed"


class HookAbort(HookError):
    """A hook deliberately stops the request with a chosen status (e.g. 403)."""

    code = "hook_abort"

    def __init__(self, message: str, *, status: int = 400,
                 code: str | None = None, field: str | None = None) -> None:
        super().__init__(message, field=field)
        self.status = status
        if code is not None:
            self.code = code


# ── Persistence layer ────────────────────────────────────────────────────────

class DataError(RelayError):
    source = "db"


class NotFoundError(DataError):
    status = 404
    code = "not_found"


class ConflictError(DataError):
    status = 409
    code = "conflict"


class IntegrityError(DataError):
    status = 422
    code = "integrity"


class AmbiguousTarget(DataError):
    status = 409
    code = "ambiguous_target"


# ── Transport layer ──────────────────────────────────────────────────────────

class RequestError(RelayError):
    source = "request"


class BadJSON(RequestError):
    status = 400
    code = "invalid_json"


class BadParam(RequestError):
    status = 400
    code = "invalid_param"


class PayloadTooLarge(RequestError):
    """Request body or bulk-row count exceeded the configured cap."""

    status = 413
    code = "payload_too_large"


__all__ = [
    "RelayError",
    "HookError", "ValidationError", "HookAbort",
    "DataError", "NotFoundError", "ConflictError", "IntegrityError", "AmbiguousTarget",
    "RequestError", "BadJSON", "BadParam", "PayloadTooLarge",
]