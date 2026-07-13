"""
arc.codec
-----------------
The Kernel's sole codec (Architecture §4: msgspec — orjson deliberately
dropped as redundant). Stateless: no project binding needed, works even
before arc.boot(). One shared implementation so plugins stop reinventing
serialization each their own way — gateway called msgspec directly in three
places, psqldb used stdlib json for its jsonb columns; both now route
through here instead of duplicating it.

    import arc
    arc.codec.encode(value)                 -> bytes
    arc.codec.decode(data)                  -> Any
    arc.codec.decode(data, type=MyStruct)   -> MyStruct, validated
    arc.codec.validate(obj, type=MyStruct)  -> MyStruct, validated (obj is
                                                already-decoded, e.g. a dict)
    arc.codec.schema(MyStruct)              -> JSON Schema dict

decode()/validate() always raise CodecError, never a raw msgspec exception
— callers never need `import msgspec` themselves just to catch a failure.
`Struct` is re-exported so a plugin can declare a schema type without that
import either.
"""

from __future__ import annotations

from typing import Any, TypeVar

import msgspec
from msgspec import Struct  # noqa: F401 - re-exported: arc.codec.Struct

T = TypeVar("T")


class CodecError(ValueError):
    """A decode/validate call failed — bad JSON, or data that doesn't match
    the given type. The one exception type every caller needs to know
    about, regardless of which underlying codec library is doing the work."""


def encode(value: Any) -> bytes:
    return msgspec.json.encode(value)


def decode(data: bytes | str, *, type: type[T] | None = None) -> T:
    """Decode raw JSON bytes/str. With `type`, decodes AND validates in one
    step (msgspec's core strength — no separate parse-then-validate pass)."""
    try:
        if type is None:
            return msgspec.json.decode(data)
        return msgspec.json.decode(data, type=type)
    except msgspec.ValidationError as exc:  # must come first: it subclasses DecodeError
        raise CodecError(f"validation failed: {exc}") from exc
    except msgspec.DecodeError as exc:
        raise CodecError(f"malformed JSON: {exc}") from exc


def validate(obj: Any, *, type: type[T]) -> T:
    """Validate/coerce an already-decoded object (e.g. a plain dict — the
    shape a schema/patch file or a psqldb row arrives as) against `type`.
    msgspec has no separate "validate this in-memory object" entry point;
    this is msgspec.convert(), named to match decode()/encode() here."""
    try:
        return msgspec.convert(obj, type=type)
    except msgspec.ValidationError as exc:
        raise CodecError(f"validation failed: {exc}") from exc


def schema(type: Any) -> dict:
    """JSON Schema for `type` — gateway.openapi's only remaining call site
    for this is arc.codec.schema() now, not msgspec.json.schema() directly."""
    return msgspec.json.schema(type)
