"""
plugins.redix.serializers
==========================
JSON-safe encode/decode for values stored in Redis. Mirrors the coercion relay
already does for asyncpg returns (UUID, date/datetime, Decimal → JSON-safe), so
a value round-trips through the cache without surprising the caller.

Values are stored as UTF-8 JSON bytes. ``encode`` accepts the common Python
types business code passes; ``decode`` returns plain JSON types (callers that
need rich types re-coerce on their side, exactly as they would for a fresh DB
read).
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid as _uuid
from decimal import Decimal as _Decimal
from typing import Any


def _default(obj: Any) -> Any:
    if isinstance(obj, _uuid.UUID):
        return str(obj)
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, _Decimal):
        # str preserves precision; float would lose it.
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    raise TypeError(f"Type {type(obj).__name__} is not cache-serializable.")


def encode(value: Any) -> bytes:
    """Serialize *value* to UTF-8 JSON bytes for storage in Redis."""
    return json.dumps(value, default=_default, separators=(",", ":")).encode("utf-8")


def decode(raw: Any) -> Any:
    """Deserialize Redis bytes/str back to a Python JSON value. ``None`` in →
    ``None`` out (a cache miss)."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)