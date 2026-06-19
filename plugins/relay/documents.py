"""
plugins.relay.documents
========================
``arc`` — the single, context-bound database API handed to handlers and hooks.

Session binding is automatic (developers never touch sessions):
  • inside a handler or pre-commit hook → the ACTIVE transaction session
    (reads see the in-flight, uncommitted write)
  • inside a post-commit hook / background task → a FRESH session
    (reads see committed state)
  • standalone (CLI / scripts) → its own short transaction

Reads outside an active transaction run on a ``read_only=True`` session (no
COMMIT round-trip).

Insert-vs-update classification is **caller-driven** (``match_on``); there is no
constraint introspection. A schema-level UNIQUE constraint is the only race-safe
uniqueness guarantee — a validate hook gives a friendly message but is TOCTOU.

Write surface:
  save(table, values, *, match_on=None)        upsert (0 match → insert, 1 → update, >1 → AmbiguousTarget)
  update(table, match, values)                 update-existing-only (0 → NotFoundError)
  save_many(table, rows, *, atomic=True, ...)   per-row upserts; atomic default
  update_many(table, filter, values, ...)       bulk update-by-filter (many rows)
  rm(table, filters) / rm_many(table, filters)  soft delete (_state = 99)

Soft delete: deleted rows carry ``_state = 99``; every method except ``arc.query``
excludes them by default (unless the caller filters on _state). Hooks NEVER fire
on read ops.
"""

from __future__ import annotations

import datetime as _dt
import re
import time as _time
from collections import defaultdict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from decimal import Decimal as _Decimal, InvalidOperation as _InvalidOperation
from typing import Any, Callable

from sqlalchemy import bindparam, text
from sqlalchemy.exc import DBAPIError, IntegrityError as SAIntegrityError

from arc.kernel.context import get_user
from arc.kernel.logger import get_logger

from plugins.relay.errors import (
    AmbiguousTarget, BadParam, ConflictError, IntegrityError, NotFoundError,
    PayloadTooLarge, ValidationError,
)
from plugins.relay.filters import (
    STATE_DELETED, build_where, ident as _ident, normalize_filters,
    order_clause, search_clause,
)
from plugins.relay.registry import Relay

log = get_logger("arc.plugin.relay.documents")

STATE_ACTIVE = 0
DEFAULT_LIST_CAP = 1000
DEFAULT_RM_MANY_CAP = 1000
DEFAULT_MAX_BULK_ROWS = 1000
DEFAULT_MAX_BODY_BYTES = 1_048_576
DEFAULT_TYPE_TTL = 60.0
_AGG_FUNCS = frozenset({"sum", "avg", "min", "max", "count"})

# System fields stripped from every GET / list response unless the caller
# explicitly lists them in `fields=`. `id` is always preserved.
_SYSTEM_STRIP_FIELDS = frozenset({
    "created_at", "updated_at", "created_by", "updated_by", "_state",
})

# Pagination modes for arc.list. The default is cursor-by-id-DESC (newest-first,
# leverages UUIDv7 time ordering). Offset mode is auto-selected when the caller
# overrides the order — cursor semantics only hold when sorting by id.
PAGINATION_CURSOR = "cursor"
PAGINATION_OFFSET = "offset"
_DEFAULT_CURSOR_ORDER = "-id"   # UUIDv7 DESC == newest first

# Arc field types whose string inputs get coerced before reaching asyncpg.
_SYSTEM_TYPES: dict[str, str] = {
    "_state": "Int",
    "created_at": "Datetime", "updated_at": "Datetime",
    "created_by": "Data", "updated_by": "Data",
    # "id" is uuid — asyncpg accepts the str form; passthrough.
}

# Request / transaction-scoped context.
_active_session: ContextVar[Any] = ContextVar("arc_active_session", default=None)
_active_tx: ContextVar["TxContext | None"] = ContextVar("arc_active_tx", default=None)
_post_commit_queue: ContextVar["list | None"] = ContextVar("arc_post_commit", default=None)


# ── value coercion (type-driven) ─────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _coerce_typed(arc_type: str | None, field: str, value: Any) -> Any:
    """Coerce a string input to the Python type asyncpg expects for *arc_type*.
    Non-strings pass through; unknown / text-like types pass through. Invalid
    values raise a friendly BadParam rather than a raw DB error."""
    if value is None or not isinstance(value, str):
        return value
    s = value.strip()
    try:
        if arc_type == "Date":
            return _dt.date.fromisoformat(s) if s else None
        if arc_type == "Datetime":
            return _dt.datetime.fromisoformat(s) if s else None
        if arc_type == "Int":
            return int(s) if s else None
        if arc_type == "Float":
            return float(s) if s else None
        if arc_type == "Decimal":
            return _Decimal(s) if s else None
        if arc_type == "Bool":
            low = s.lower()
            if low in ("true", "1", "yes", "y", "t"):
                return True
            if low in ("false", "0", "no", "n", "f"):
                return False
            raise ValueError("not a boolean")
        if arc_type == "Email":
            if s and not _EMAIL_RE.match(s):
                raise ValueError("not a valid email address")
            return s
    except (ValueError, _InvalidOperation):
        raise BadParam(f"{field!r}: invalid {arc_type} value {value!r}.")
    # Data / Text / JSON / Link / Password / Table / unknown → passthrough.
    # Passwords are NOT transformed here; stripping happens at the response layer.
    return value


# ── DB error mapping (SQLSTATE → generic, source-segregated) ─────────────────

def _sqlstate(exc: Exception) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


_SQLSTATE_MAP: dict[str, tuple[str, type]] = {
    "23505": ("A row with these unique values already exists.", ConflictError),
    "23503": ("References a row that does not exist.", ConflictError),
    "23502": ("A required field is missing.", IntegrityError),
    "23514": ("A value failed a database constraint.", IntegrityError),
    "22P02": ("A value has an invalid format.", IntegrityError),
    "22007": ("A date/time value is invalid.", IntegrityError),
    "22008": ("A date/time value is out of range.", IntegrityError),
    "22003": ("A numeric value is out of range.", IntegrityError),
}


def _raise_db_error(exc: Exception) -> None:
    """Log the raw DB error server-side; raise a generic client-safe error.
    Never echoes raw asyncpg text to the client."""
    code = _sqlstate(exc)
    raw = str(getattr(exc, "orig", exc))
    log.warning("arc.relay.db_error", sqlstate=code, error=raw.split("\n")[0][:300])
    msg, cls = _SQLSTATE_MAP.get(code, ("The database rejected the row.", IntegrityError))
    raise cls(msg) from exc


def _current_user() -> str | None:
    u = get_user()
    return getattr(u, "id", None) if u else None


# ── arc.query read-only guard ────────────────────────────────────────────────

def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _strip_sql_literals(sql: str) -> str:
    """Remove single-quoted strings and double-quoted identifiers so keyword
    scanning never trips on a literal like 'please update later'."""
    sql = re.sub(r"'(?:''|[^'])*'", " ", sql)
    sql = re.sub(r'"(?:[^"])*"', " ", sql)
    return sql


_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|merge|truncate|create|drop|alter|grant)\b", re.I)


def _assert_single_select(sql: str) -> None:
    s = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not s:
        raise BadParam("Empty query.")
    if ";" in s:
        raise BadParam("arc.query allows a single statement only.")
    head = s.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        raise BadParam("arc.query allows read-only SELECT / WITH statements only.")


def _assert_no_write_keywords(sql: str) -> None:
    """Statement-level guard used when arc.query runs inside an active txn (a
    hook), where a read-only transaction isn't available. Defense-in-depth only:
    never pass untrusted input into arc.query SQL regardless of this guard."""
    scrubbed = _strip_sql_literals(_strip_sql_comments(sql))
    m = _WRITE_KEYWORDS.search(scrubbed)
    if m:
        raise BadParam(
            f"arc.query rejected: statement contains a write keyword "
            f"({m.group(1).upper()}). Inside a hook, arc.query is read-only.")


def _skip_set(local_vars: dict) -> set[str]:
    """Turn skip_* keyword flags into a set of event names."""
    return {k[len("skip_"):] for k, v in local_vars.items()
            if k.startswith("skip_") and v is True}


def _skip_kwargs(skip: set[str]) -> dict:
    """Rebuild skip_* kwargs from a skip set (for delegating to save())."""
    return {f"skip_{e}": True for e in skip}


# ── Transaction scratch object ───────────────────────────────────────────────

class TxContext:
    """In-memory metadata carried for the lifetime of one transaction and passed
    to on_commit(tx) / on_rollback(tx). No SQL surface — it never touches the DB,
    so it disappears automatically on rollback. ``_pending`` holds per-doc
    on_change payloads; the boundary flushes them only after a real commit."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._collected: dict[str, list] = defaultdict(list)
        self._pending: list[tuple] = []
        self.error: Exception | None = None

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def collect(self, key: str, value: Any) -> None:
        self._collected[key].append(value)

    def collected(self, key: str) -> list:
        return list(self._collected.get(key, []))

    def _add_change(self, table, event, data, previous, user) -> None:
        self._pending.append((table, event, data, previous, user))


# ── Document handed to hooks ─────────────────────────────────────────────────

class _Old:
    """Null-object view of the prior row. ``doc.old.field`` is None on insert."""

    __slots__ = ("_d",)

    def __init__(self, data: dict | None) -> None:
        object.__setattr__(self, "_d", dict(data) if data else {})

    def __getattr__(self, name: str) -> Any:
        return self._d.get(name)

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def __bool__(self) -> bool:
        return bool(self._d)


_DOC_INTERNAL = {"table", "event", "old", "is_new", "user"}


class Document:
    """Single argument passed to every document hook.

      doc.<field>        current / live value (mutable in before_* hooks)
      doc.old.<field>    prior value (null-object: None on insert)
      doc.is_new         True on insert
      doc.event          "insert" | "update" | "delete"
    """

    def __init__(self, table: str, event: str, data: dict,
                 old: dict | None, user: str | None) -> None:
        object.__setattr__(self, "table", table)
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "_data", dict(data))
        object.__setattr__(self, "old", _Old(old))
        object.__setattr__(self, "is_new", event == "insert")
        object.__setattr__(self, "user", user)

    def __getattr__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _DOC_INTERNAL or name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value

    def get(self, field: str, default: Any = None) -> Any:
        return self._data.get(field, default)

    def set(self, field: str, value: Any) -> None:
        self._data[field] = value

    def as_dict(self) -> dict:
        return dict(self._data)

    def fail(self, message: str, *, field: str | None = None) -> None:
        raise ValidationError(message, field=field)

    def require(self, field: str, message: str | None = None) -> None:
        val = self._data.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            raise ValidationError(message or f"{field} is required.", field=field)

    def changed(self, field: str) -> bool:
        return self._data.get(field) != self.old.get(field)


# ── The arc gateway ──────────────────────────────────────────────────────────

class Arc:
    """The single database API. A process-wide singleton; the session factory
    and registrar are injected by the relay plugin at setup()."""

    def __init__(self) -> None:
        self._cm: Callable | None = None
        self._reg: Relay | None = None
        self.list_cap = DEFAULT_LIST_CAP
        self.rm_many_cap = DEFAULT_RM_MANY_CAP
        self.max_bulk_rows = DEFAULT_MAX_BULK_ROWS
        self.max_body_bytes = DEFAULT_MAX_BODY_BYTES
        self._type_ttl = DEFAULT_TYPE_TTL
        self._meta_cache: dict[str, tuple[float, dict]] = {}

    def _bind(self, session_cm: Callable, registrar: Relay, *,
              list_cap: int | None = None, rm_many_cap: int | None = None,
              max_bulk_rows: int | None = None, max_body_bytes: int | None = None,
              type_ttl: float | None = None) -> None:
        self._cm = session_cm
        self._reg = registrar
        if list_cap:
            self.list_cap = list_cap
        if rm_many_cap:
            self.rm_many_cap = rm_many_cap
        if max_bulk_rows:
            self.max_bulk_rows = max_bulk_rows
        if max_body_bytes:
            self.max_body_bytes = max_body_bytes
        if type_ttl:
            self._type_ttl = type_ttl

    def _ready(self) -> None:
        if self._cm is None or self._reg is None:
            raise RuntimeError("arc is not initialised — relay plugin not set up yet.")

    def reset_caches(self) -> None:
        """Drop the per-table metadata cache. Call from the dev file-watcher on
        reload, or after `arc psqldb migrate` while the process is up."""
        self._meta_cache.clear()

    # ── session resolution ──────────────────────────────────────────────
    @asynccontextmanager
    async def _read_session(self):
        """Active txn session if present (sees uncommitted writes), else a fresh
        read-only session that skips the COMMIT round-trip."""
        self._ready()
        sess = _active_session.get()
        if sess is not None:
            yield sess
        else:
            async with self._cm(read_only=True) as s:
                yield s

    @asynccontextmanager
    async def _boundary(self):
        """Yield (session, tx, owns). Join an outer arc.tx() if active; otherwise
        open an implicit single-write boundary that commits and fires
        on_commit / on_rollback once."""
        self._ready()
        outer = _active_tx.get()
        if outer is not None:
            yield _active_session.get(), outer, False
            return

        tx_obj = TxContext()
        committed = False
        async with self._cm() as session:
            stok = _active_session.set(session)
            ttok = _active_tx.set(tx_obj)
            try:
                yield session, tx_obj, True
                await session.commit()
                committed = True
            except Exception as exc:
                await session.rollback()
                tx_obj.error = exc
            finally:
                _active_session.reset(stok)
                _active_tx.reset(ttok)

        if committed:
            for args in tx_obj._pending:
                await self._dispatch_post(self._fire_on_change(*args))
            await self._dispatch_post(self._fire_tx("on_commit", tx_obj))
        else:
            await self._dispatch_post(self._fire_tx("on_rollback", tx_obj))
            raise tx_obj.error

    @asynccontextmanager
    async def tx(self):
        """Group several writes into one commit / rollback boundary."""
        async with self._boundary() as (_session, tx_obj, _owns):
            yield tx_obj

    # ── column metadata (cached, TTL-bounded) ───────────────────────────
    async def _meta(self, table: str) -> dict:
        """Per-table metadata from _field_registry, TTL-cached. Shape:

            {
              "types":         {field: Arc type},   # incl. system fields
              "passwords":     {field, ...},        # fields to strip from responses
              "incoming_links": [                   # OTHER tables that point at us
                  {"table": str, "field": str},     #   via type="Link"
              ],
              "table_children": [                   # rows we own (cascade-delete)
                  {"table": str},                   #   declared as type="Table"
              ],
            }

        Read on a FRESH read-only session so a registry failure (e.g. pre-migrate)
        never poisons an active write transaction.
        """
        now = _time.monotonic()
        hit = self._meta_cache.get(table)
        if hit and hit[0] > now:
            return hit[1]

        types = dict(_SYSTEM_TYPES)
        passwords: set[str] = set()
        incoming: list[dict] = []
        children: list[dict] = []
        link_index: dict[str, str] = {}   # field_name → link_table (own Link fields)
        try:
            async with self._cm(read_only=True) as s:
                # Own fields
                rows = (await s.execute(
                    text("SELECT field_name, type, is_virtual, link_table "
                         "FROM _field_registry WHERE table_name = :t"),
                    {"t": table})).all()
                for fname, atype, is_virt, lt in rows:
                    types[str(fname)] = str(atype)
                    if atype == "Password":
                        passwords.add(str(fname))
                    if atype == "Link" and lt:
                        link_index[str(fname)] = str(lt)
                    if atype == "Table" and is_virt and lt:
                        children.append({"table": str(lt)})
                # Fields in OTHER tables that reference us via Link
                rows = (await s.execute(
                    text("SELECT table_name, field_name FROM _field_registry "
                         "WHERE link_table = :t AND type = 'Link' "
                         "AND is_virtual = false"),
                    {"t": table})).all()
                for ref_table, ref_field in rows:
                    incoming.append({"table": str(ref_table), "field": str(ref_field)})
        except Exception as exc:  # registry missing (pre-migrate) → system types only
            log.debug("arc.relay.registry_unavailable", table=table, error=str(exc))

        meta = {"types": types, "passwords": passwords,
                "incoming_links": incoming, "table_children": children,
                "_link_table_index": link_index}
        self._meta_cache[table] = (now + self._type_ttl, meta)
        return meta

    async def _types(self, table: str) -> dict[str, str]:
        """table → {field: Arc type}. Back-compat shim for older call sites."""
        return (await self._meta(table))["types"]

    # ── response stripping (system fields + passwords) ──────────────────
    def _strip_response(self, row: dict, *, table_passwords: set[str],
                        keep_extra: set[str]) -> dict:
        """Strip system fields (except id and anything in keep_extra) AND every
        Password-type field. Returns a fresh dict; never mutates the input.

        Hooks always see the full row in memory — this is the LAST step before
        a dict crosses the HTTP boundary."""
        out: dict = {}
        for k, v in row.items():
            if k in table_passwords:
                continue
            if k == "id" or k in keep_extra:
                out[k] = v
                continue
            if k in _SYSTEM_STRIP_FIELDS:
                continue
            out[k] = v
        return out

    async def _strip_rows(self, table: str, rows: list[dict],
                          fields: list[str] | None) -> list[dict]:
        """Apply _strip_response to a list. ``fields=None`` (no projection)
        means strip all system fields; an explicit ``fields=[...]`` lets system
        fields through if listed there."""
        meta = await self._meta(table)
        keep = set(fields or ())
        pw = meta["passwords"]
        return [self._strip_response(r, table_passwords=pw, keep_extra=keep)
                for r in rows]

    # ── referential checks for deletes ──────────────────────────────────
    async def _check_referential(self, session, table: str, ids: list) -> None:
        """Block delete if any other table holds an active Link to any of *ids*.
        Raises ConflictError on the first referencing table found.
        Applies to ALL delete paths (rm, rm_many, _bulk_soft_delete) — only
        arc.query is exempt."""
        if not ids:
            return
        meta = await self._meta(table)
        for ref in meta["incoming_links"]:
            ref_table = ref["table"]
            ref_field = ref["field"]
            stmt = text(
                f'SELECT {_ident(ref_field)} FROM {_ident(ref_table)} '
                f'WHERE {_ident(ref_field)} IN :ids '
                f'AND "_state" != {STATE_DELETED} LIMIT 1'
            ).bindparams(bindparam("ids", expanding=True))
            row = (await session.execute(stmt, {"ids": list(ids)})).first()
            if row is not None:
                raise ConflictError(
                    f"Cannot delete {table} — referenced by {ref_table}."
                    f"{ref_field}.",
                )

    async def _cascade_children(self, session, table: str, ids: list,
                                 user) -> None:
        """For every Table-type child relationship declared on *table*, soft-delete
        all active child rows whose Link field equals one of *ids*. Called before
        the parent row is soft-deleted. Cascade is ONE LEVEL — a grandchild whose
        deletion would itself be blocked surfaces as a ConflictError on the
        parent delete via the block check below."""
        if not ids:
            return
        meta = await self._meta(table)
        if not meta["table_children"]:
            return

        for child in meta["table_children"]:
            child_table = child["table"]
            # Find which field on the child table is the Link back to us.
            child_meta = await self._meta(child_table)
            link_back_fields = [
                fname for fname, atype in child_meta["types"].items()
                if atype == "Link" and self._link_target(child_meta, fname) == table
            ]
            if not link_back_fields:
                # Declared parent→child but child has no Link back — log and skip.
                log.warning("arc.relay.cascade_no_link_back",
                            parent=table, child=child_table)
                continue
            for fname in link_back_fields:
                # Find every child id that would be cascade-deleted, then run
                # the block check against THOSE ids (one-level deep).
                child_ids_stmt = text(
                    f'SELECT "id" FROM {_ident(child_table)} '
                    f'WHERE {_ident(fname)} IN :ids '
                    f'AND "_state" != {STATE_DELETED}'
                ).bindparams(bindparam("ids", expanding=True))
                child_ids = [r[0] for r in (
                    await session.execute(child_ids_stmt, {"ids": list(ids)})
                ).all()]
                if not child_ids:
                    continue
                await self._check_referential(session, child_table, child_ids)
                # Soft-delete the children. Hooks NOT fired for cascade rows
                # (matches the soft-delete fast path).
                stmt = text(
                    f'UPDATE {_ident(child_table)} SET '
                    f'"_state" = {STATE_DELETED}, "updated_by" = :u, '
                    f'"updated_at" = now() WHERE "id" IN :cids '
                    f'AND "_state" != {STATE_DELETED}'
                ).bindparams(bindparam("cids", expanding=True))
                await session.execute(stmt, {"cids": child_ids, "u": user})
                log.info("arc.relay.cascade_deleted", parent=table,
                         child=child_table, rows=len(child_ids))

    @staticmethod
    def _link_target(meta: dict, field_name: str) -> str | None:
        """Resolve the link_table for a given Link field on a table whose
        metadata is already loaded. Returns None when the registry has no row
        for that field (drift)."""
        # meta only carries types and counts here; the link target is in
        # _field_registry. The caller can wire its own lookup if needed.
        # For the cascade pre-check we already filtered to type == "Link", and
        # the absence of a link_table value is rare; treat as None.
        return meta.get("_link_table_index", {}).get(field_name)

    def _coerce_payload(self, types: dict[str, str], data: dict) -> dict:
        """Project a write payload: drop _-prefixed + id, coerce by column type."""
        return {k: _coerce_typed(types.get(k), k, v)
                for k, v in data.items() if not k.startswith("_") and k != "id"}

    # ── READ OPS (no hooks) ─────────────────────────────────────────────
    async def get(self, table: str, filters) -> dict | None:
        rows = await self.list(table, fields=None, filters=filters,
                               order=_DEFAULT_CURSOR_ORDER, limit=1)
        return rows[0] if rows else None

    async def list(self, table: str, *, fields: list[str] | None = None,
                   filters=None, order: str | None = None,
                   limit: int | None = None, offset: int | None = None,
                   cursor: str | None = None,
                   search: tuple[list[str], str] | None = None) -> list[dict]:
        """Matching docs as a flat list (no pagination envelope).

        Pagination behaviour:
          • ``cursor`` provided → cursor-by-id mode (works only with default order).
          • ``offset`` provided → offset mode.
          • ``order`` explicitly set to anything other than the default cursor
            order → offset mode (cursor is only correct when ordering by id).
          • Otherwise → cursor-by-id DESC default, no cursor (first page).

        Hooks see the full row. System fields (created_at, updated_at,
        created_by, updated_by, _state) are stripped from each returned dict
        unless explicitly listed in *fields*. Password-type fields are always
        stripped — they cannot be opted back in via *fields*.
        """
        page = await self.list_page(
            table, fields=fields, filters=filters, order=order,
            limit=limit, offset=offset, cursor=cursor, search=search,
        )
        return page["data"]

    async def list_page(self, table: str, *, fields: list[str] | None = None,
                        filters=None, order: str | None = None,
                        limit: int | None = None, offset: int | None = None,
                        cursor: str | None = None,
                        search: tuple[list[str], str] | None = None) -> dict:
        """list() with the pagination envelope.

        Cursor mode response:
          {"data": [...], "next_cursor": "<uuid>" | None, "has_more": bool}
        Offset mode response:
          {"data": [...], "total": N, "offset": M, "limit": L}
        """
        # Decide pagination mode.
        effective_order = order or _DEFAULT_CURSOR_ORDER
        use_cursor = (
            order is None and offset is None
        ) or (order == _DEFAULT_CURSOR_ORDER and offset is None)
        # cursor= value overrides → cursor mode even on first page.
        if cursor is not None:
            use_cursor = True
            effective_order = _DEFAULT_CURSOR_ORDER

        # Projection. SQL still selects all so hooks can use the full row, but
        # the response-strip phase below honours the caller's projection intent.
        sql_cols = "*"
        if fields:
            wanted = list(dict.fromkeys(["id", *fields]))
            sql_cols = ", ".join(_ident(c) for c in wanted)

        params: dict = {}
        where = build_where(normalize_filters(filters), params, exclude_deleted=True)
        if search and search[0] and search[1]:
            where = f"({where}) AND {search_clause(search[0], search[1], params)}"

        # Cursor predicate: WHERE id < :cursor (for DESC).
        if use_cursor and cursor:
            params["__cursor"] = cursor
            comp = "<" if effective_order.startswith("-") else ">"
            where = f"({where}) AND \"id\" {comp} :__cursor"

        capped_limit = int(limit if limit is not None else self.list_cap)
        capped_limit = max(1, min(capped_limit, self.list_cap))

        # Fetch limit+1 in cursor mode so we know if there's a next page.
        sql_limit = capped_limit + 1 if use_cursor else capped_limit
        sql = (f"SELECT {sql_cols} FROM {_ident(table)} WHERE {where} "
               f"ORDER BY {order_clause(effective_order)} LIMIT {sql_limit}")
        if not use_cursor and offset:
            sql += f" OFFSET {int(offset)}"

        async with self._read_session() as s:
            rows = [dict(r) for r in (await s.execute(text(sql), params)).mappings().all()]

        stripped = await self._strip_rows(table, rows, fields)

        if use_cursor:
            has_more = len(stripped) > capped_limit
            data = stripped[:capped_limit]
            next_cursor = str(data[-1]["id"]) if (has_more and data) else None
            return {"data": data, "next_cursor": next_cursor, "has_more": has_more}

        # Offset mode — also compute total so the client can paginate.
        total = await self.count(table, filters)
        return {"data": stripped, "total": total,
                "offset": int(offset or 0), "limit": capped_limit}

    async def exists(self, table: str, filters) -> bool:
        params: dict = {}
        where = build_where(normalize_filters(filters), params, exclude_deleted=True)
        sql = f"SELECT 1 FROM {_ident(table)} WHERE {where} LIMIT 1"
        async with self._read_session() as s:
            return (await s.execute(text(sql), params)).first() is not None

    async def count(self, table: str, filters=None) -> int:
        params: dict = {}
        where = build_where(normalize_filters(filters), params, exclude_deleted=True)
        sql = f"SELECT COUNT(*) FROM {_ident(table)} WHERE {where}"
        async with self._read_session() as s:
            return int((await s.execute(text(sql), params)).scalar() or 0)

    async def aggregate(self, table: str, *, fn: str, field: str, where=None) -> Any:
        if fn not in _AGG_FUNCS:
            raise BadParam(f"aggregate fn must be one of {sorted(_AGG_FUNCS)}, got {fn!r}.")
        params: dict = {}
        wsql = build_where(normalize_filters(where), params, exclude_deleted=True)
        sql = f"SELECT {fn.upper()}({_ident(field)}) FROM {_ident(table)} WHERE {wsql}"
        async with self._read_session() as s:
            return (await s.execute(text(sql), params)).scalar()

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        """Raw READ-ONLY SELECT for joins / dashboards. Parameterized, single
        statement. Does NOT apply the _state filter — the caller owns it.

        Outside a txn → runs in a real READ ONLY transaction (Postgres rejects
        every write, including data-modifying CTEs). Inside a hook/txn → a
        read-only txn isn't available, so a keyword guard applies instead.
        Never interpolate untrusted input into the SQL regardless of guard."""
        _assert_single_select(sql)
        active = _active_session.get()
        if active is not None:
            _assert_no_write_keywords(sql)
            rows = (await active.execute(text(sql), params or {})).mappings().all()
            return [dict(r) for r in rows]
        async with self._cm(read_only=True) as s:
            await s.execute(text("SET TRANSACTION READ ONLY"))
            rows = (await s.execute(text(sql), params or {})).mappings().all()
            return [dict(r) for r in rows]

    # ── WRITE: save (upsert) ────────────────────────────────────────────
    def _match_from(self, values: dict, match_on) -> dict | None:
        """Build the match filter. Explicit match_on → all keys required.
        match_on=None → match by 'id' if present, else None (insert)."""
        if match_on:
            missing = [k for k in match_on if values.get(k) in (None, "")]
            if missing:
                raise BadParam(f"match_on requires non-empty {', '.join(missing)}.")
            return {k: values[k] for k in match_on}
        if values.get("id"):
            return {"id": values["id"]}
        return None

    async def _find_one(self, session, table: str, match: dict) -> dict | None:
        """≤1 match contract: >1 → AmbiguousTarget; 0 → None."""
        params: dict = {}
        where = build_where(normalize_filters(match), params, exclude_deleted=True)
        stmt = text(f"SELECT * FROM {_ident(table)} WHERE {where} "
                    f'ORDER BY {order_clause("-updated_at")} LIMIT 2')
        rows = (await session.execute(stmt, params)).mappings().all()
        if len(rows) > 1:
            raise AmbiguousTarget(f"{table}: match filter matched multiple rows.")
        return dict(rows[0]) if rows else None

    async def save(self, table: str, values: dict, *, match_on=None,
                   skip_validate=False, skip_before_insert=False, skip_after_insert=False,
                   skip_before_update=False, skip_after_update=False,
                   skip_on_change=False) -> dict:
        """Upsert. Classify via the caller's ``match_on`` (or id). >1 match raises
        AmbiguousTarget — use save_many / update_many for multi-row writes."""
        skip = _skip_set(locals())
        user = _current_user()
        async with self._boundary() as (session, tx_obj, _owns):
            match = self._match_from(values, match_on)
            old = await self._find_one(session, table, match) if match else None
            event = "update" if old else "insert"
            doc = Document(table, event, values, old, user)

            await self._fire_pre(table, "validate", doc, skip)
            if event == "insert":
                await self._fire_pre(table, "before_insert", doc, skip)
                row = await self._do_insert(session, table, doc.as_dict(), user)
                for k, v in row.items():
                    doc.set(k, v)
                await self._fire_pre(table, "after_insert", doc, skip)
            else:
                await self._fire_pre(table, "before_update", doc, skip)
                row = await self._do_update(session, table, old["id"], doc.as_dict(), user)
                for k, v in row.items():
                    doc.set(k, v)
                await self._fire_pre(table, "after_update", doc, skip)

            if "on_change" not in skip and self._reg.hooks_for(table, "on_change"):
                tx_obj._add_change(table, event, dict(row), old, user)
        return dict(row)

    # ── WRITE: update (existing only) ───────────────────────────────────
    async def update(self, table: str, match, values: dict, *,
                      skip_validate=False, skip_before_update=False,
                      skip_after_update=False, skip_on_change=False) -> dict:
        """Update an existing row matched by ``match``. Never inserts.
        0 matches → NotFoundError (404). >1 → AmbiguousTarget. Only the fields
        in ``values`` are written (natural partial update)."""
        skip = _skip_set(locals())
        user = _current_user()
        async with self._boundary() as (session, tx_obj, _owns):
            old = await self._find_one(session, table, dict(match))
            if old is None:
                raise NotFoundError(f"{table}: no row matches the filter for update.")
            doc = Document(table, "update", values, old, user)
            await self._fire_pre(table, "validate", doc, skip)
            await self._fire_pre(table, "before_update", doc, skip)
            row = await self._do_update(session, table, old["id"], doc.as_dict(), user)
            for k, v in row.items():
                doc.set(k, v)
            await self._fire_pre(table, "after_update", doc, skip)
            if "on_change" not in skip and self._reg.hooks_for(table, "on_change"):
                tx_obj._add_change(table, "update", dict(row), old, user)
        return dict(row)

    # ── WRITE: save_many (per-row upserts) ──────────────────────────────
    async def save_many(self, table: str, rows: list[dict], *, match_on=None,
                        atomic: bool = True, isolated_rows: bool = False,
                        skip_validate=False, skip_before_insert=False,
                        skip_after_insert=False, skip_before_update=False,
                        skip_after_update=False, skip_on_change=False):
        """Upsert many DISTINCT docs; each row independently matches ≤1.

        atomic=True (default): one transaction, all-or-nothing; on any row failure
          the batch rolls back and the error carries the row index.
        atomic=False: per-row independent commits → {"saved": [...], "errors": [...]}.
        isolated_rows=True: when the table has no write hooks, all-insert batches use
          a single multi-row INSERT (rows must not depend on each other)."""
        self._ready()
        if len(rows) > self.max_bulk_rows:
            raise PayloadTooLarge(
                f"save_many accepts at most {self.max_bulk_rows} rows ({len(rows)} given).")
        skip = _skip_set(locals())
        user = _current_user()

        if not atomic:
            saved, errors = [], []
            for i, row in enumerate(rows):
                try:
                    saved.append(await self.save(table, row, match_on=match_on,
                                                 **_skip_kwargs(skip)))
                except Exception as exc:  # noqa: BLE001
                    errors.append({"index": i, "detail": str(exc), "row": row})
            return {"saved": saved, "errors": errors}

        # atomic
        no_hooks = not self._reg.has_any_hooks(
            table, ("validate", "before_insert", "after_insert",
                    "before_update", "after_update", "on_change"))
        if isolated_rows and no_hooks and rows:
            return await self._save_many_fast(table, rows, match_on, user)

        saved = []
        async with self._boundary():
            for i, row in enumerate(rows):
                try:
                    saved.append(await self.save(table, row, match_on=match_on,
                                                 **_skip_kwargs(skip)))
                except ValidationError as exc:
                    exc.message = f"row {i}: {exc.message}"
                    raise
                except (ConflictError, IntegrityError, NotFoundError, AmbiguousTarget) as exc:
                    exc.message = f"row {i}: {exc.message}"
                    raise
        return saved

    async def _save_many_fast(self, table, rows, match_on, user) -> list[dict]:
        """No-hook batched path. All-insert → one multi-row INSERT; otherwise
        per-row within one txn (still atomic)."""
        all_insert = (not match_on) and all(not r.get("id") for r in rows)
        async with self._boundary() as (session, _tx, _owns):
            if all_insert:
                return await self._bulk_insert(session, table, rows, user)
            out = []
            for r in rows:
                match = self._match_from(r, match_on)
                old = await self._find_one(session, table, match) if match else None
                if old:
                    out.append(await self._do_update(session, table, old["id"], r, user))
                else:
                    out.append(await self._do_insert(session, table, r, user))
            return out

    # ── WRITE: update_many (bulk update by filter) ──────────────────────
    async def update_many(self, table: str, filter, values: dict, *,
                          order: str = "-updated_at", limit: int | None = None,
                          skip_validate=False, skip_before_update=False,
                          skip_after_update=False, skip_on_change=False) -> int:
        """Update EVERY row matching ``filter`` (one filter → many rows), capped
        at max_bulk_rows. No hooks → one UPDATE; hooks → batched read + per-row
        write. Returns the affected count."""
        skip = _skip_set(locals())
        user = _current_user()
        cap = min(limit or self.max_bulk_rows, self.max_bulk_rows)
        no_hooks = not self._reg.has_any_hooks(
            table, ("validate", "before_update", "after_update", "on_change"))
        async with self._boundary() as (session, tx_obj, _owns):
            ids = await self._match_ids(session, table, filter, limit=cap, order=order)
            if not ids:
                return 0
            if no_hooks:
                types = await self._types(table)
                payload = self._coerce_payload(types, values)
                if not payload:
                    raise ValidationError("No writable fields to update.")
                payload["updated_by"] = user
                sets = ", ".join(f"{_ident(k)} = :{k}" for k in payload)
                stmt = text(
                    f'UPDATE {_ident(table)} SET {sets}, "updated_at" = now() '
                    f'WHERE "id" IN :ids AND "_state" != {STATE_DELETED} RETURNING "id"'
                ).bindparams(bindparam("ids", expanding=True))
                try:
                    res = (await session.execute(stmt, {**payload, "ids": ids})).all()
                except SAIntegrityError as exc:
                    _raise_db_error(exc)
                except DBAPIError as exc:
                    _raise_db_error(exc)
                return len(res)

            current = await self._fetch_many(session, table, ids)
            count = 0
            for rid in ids:
                cur = current.get(rid)
                if cur is None:
                    continue
                doc = Document(table, "update", values, cur, user)
                await self._fire_pre(table, "validate", doc, skip)
                await self._fire_pre(table, "before_update", doc, skip)
                row = await self._do_update(session, table, rid, doc.as_dict(), user)
                for k, v in row.items():
                    doc.set(k, v)
                await self._fire_pre(table, "after_update", doc, skip)
                if "on_change" not in skip and self._reg.hooks_for(table, "on_change"):
                    tx_obj._add_change(table, "update", dict(row), cur, user)
                count += 1
            return count

    # ── DELETE OPS (soft, hooks fire) ───────────────────────────────────
    async def rm(self, table: str, filters, *,
                 skip_validate=False, skip_before_delete=False,
                 skip_after_delete=False, skip_on_change=False) -> dict | None:
        """Soft-delete ONE doc. Ambiguous filter → raise. None found → None.
        Blocks if any other table holds an active Link reference to the row.
        Cascade-soft-deletes Table-type children first."""
        skip = _skip_set(locals())
        user = _current_user()
        result: dict | None = None
        async with self._boundary() as (session, tx_obj, _owns):
            matches = await self._match_ids(session, table, filters, limit=2)
            if not matches:
                result = None
            elif len(matches) > 1:
                raise AmbiguousTarget(
                    f"{table}: filter matched multiple rows; rm targets exactly one.")
            else:
                # Referential checks BEFORE any write.
                await self._check_referential(session, table, [matches[0]])
                await self._cascade_children(session, table, [matches[0]], user)
                current = await self._fetch_by_id(session, table, matches[0])
                if current is None:
                    result = None
                else:
                    row = await self._delete_loaded(session, table, current, user, skip)
                    if "on_change" not in skip and self._reg.hooks_for(table, "on_change"):
                        tx_obj._add_change(table, "delete", row, row, user)
                    result = row
        return result

    async def rm_many(self, table: str, filters, *, order: str = "-updated_at",
                      limit: int | None = None,
                      skip_validate=False, skip_before_delete=False,
                      skip_after_delete=False, skip_on_change=False) -> int:
        """Soft-delete ALL matching docs (capped). No delete hooks → a single
        bulk UPDATE; hooks → batched read + per-row write. Both paths run the
        referential block check and the Table-type cascade — there is no
        fast-path escape."""
        skip = _skip_set(locals())
        user = _current_user()
        cap = min(limit or self.rm_many_cap, self.rm_many_cap)
        no_hooks = not self._reg.has_any_hooks(
            table, ("validate", "before_delete", "after_delete", "on_change"))
        async with self._boundary() as (session, tx_obj, _owns):
            ids = await self._match_ids(session, table, filters, limit=cap, order=order)
            if not ids:
                return 0

            # Referential checks BEFORE any write — batched, one query per
            # referencing field regardless of len(ids).
            await self._check_referential(session, table, ids)
            await self._cascade_children(session, table, ids, user)

            if no_hooks:
                rows = await self._bulk_soft_delete(session, table, ids, user)
                return len(rows)

            current = await self._fetch_many(session, table, ids)
            count = 0
            for rid in ids:
                cur = current.get(rid)
                if cur is None:
                    continue
                row = await self._delete_loaded(session, table, cur, user, skip)
                if "on_change" not in skip and self._reg.hooks_for(table, "on_change"):
                    tx_obj._add_change(table, "delete", row, row, user)
                count += 1
            return count

    # ── low-level SQL ───────────────────────────────────────────────────
    async def _match_ids(self, session, table: str, filters, *, limit: int,
                         order: str = "-updated_at") -> list:
        params: dict = {}
        where = build_where(normalize_filters(filters), params, exclude_deleted=True)
        stmt = text(f'SELECT "id" FROM {_ident(table)} WHERE {where} '
                    f"ORDER BY {order_clause(order)} LIMIT {int(limit)}")
        return [r[0] for r in (await session.execute(stmt, params)).all()]

    async def _fetch_by_id(self, session, table: str, row_id) -> dict | None:
        stmt = text(
            f'SELECT * FROM {_ident(table)} '
            f'WHERE "id" = :id AND "_state" != {STATE_DELETED} LIMIT 1'
        )
        row = (await session.execute(stmt, {"id": row_id})).mappings().first()
        return dict(row) if row else None

    async def _fetch_many(self, session, table: str, ids: list) -> dict:
        """{id: row} for active rows. One SELECT via expanding IN."""
        stmt = text(
            f'SELECT * FROM {_ident(table)} '
            f'WHERE "id" IN :ids AND "_state" != {STATE_DELETED}'
        ).bindparams(bindparam("ids", expanding=True))
        rows = (await session.execute(stmt, {"ids": ids})).mappings().all()
        return {r["id"]: dict(r) for r in rows}

    async def _do_insert(self, session, table, data, user) -> dict:
        types = await self._types(table)
        payload = self._coerce_payload(types, data)
        payload.setdefault("created_by", user)
        payload.setdefault("updated_by", user)
        if not payload:
            raise ValidationError("No writable fields to insert.")
        cols = ", ".join(_ident(k) for k in payload)
        binds = ", ".join(f":{k}" for k in payload)
        stmt = text(f"INSERT INTO {_ident(table)} ({cols}) VALUES ({binds}) RETURNING *")
        try:
            row = (await session.execute(stmt, payload)).mappings().first()
        except SAIntegrityError as exc:
            _raise_db_error(exc)
        except DBAPIError as exc:
            _raise_db_error(exc)
        return dict(row)

    async def _bulk_insert(self, session, table, rows, user) -> list[dict]:
        """One multi-row INSERT when all rows share a column set; else per-row."""
        types = await self._types(table)
        payloads = []
        for r in rows:
            p = self._coerce_payload(types, r)
            p.setdefault("created_by", user)
            p.setdefault("updated_by", user)
            if not p:
                raise ValidationError("No writable fields to insert.")
            payloads.append(p)

        cols = list(payloads[0].keys())
        if any(set(p.keys()) != set(cols) for p in payloads):
            return [await self._do_insert(session, table, r, user) for r in rows]

        colnames = ", ".join(_ident(c) for c in cols)
        clauses, params = [], {}
        for i, p in enumerate(payloads):
            binds = []
            for c in cols:
                key = f"r{i}_{c}"
                params[key] = p[c]
                binds.append(f":{key}")
            clauses.append(f"({', '.join(binds)})")
        stmt = text(f"INSERT INTO {_ident(table)} ({colnames}) "
                    f"VALUES {', '.join(clauses)} RETURNING *")
        try:
            result = (await session.execute(stmt, params)).mappings().all()
        except SAIntegrityError as exc:
            _raise_db_error(exc)
        except DBAPIError as exc:
            _raise_db_error(exc)
        return [dict(r) for r in result]

    async def _do_update(self, session, table, row_id, data, user) -> dict:
        types = await self._types(table)
        payload = self._coerce_payload(types, data)
        payload["updated_by"] = user
        if len(payload) == 1:  # only updated_by → nothing to write
            raise ValidationError("No writable fields to update.")
        sets = ", ".join(f"{_ident(k)} = :{k}" for k in payload)
        params = {**payload, "id": row_id}
        stmt = text(
            f'UPDATE {_ident(table)} SET {sets}, "updated_at" = now() '
            f'WHERE "id" = :id AND "_state" != {STATE_DELETED} RETURNING *'
        )
        try:
            row = (await session.execute(stmt, params)).mappings().first()
        except SAIntegrityError as exc:
            _raise_db_error(exc)
        except DBAPIError as exc:
            _raise_db_error(exc)
        if row is None:
            raise NotFoundError(f"{table} {row_id} not found.")
        return dict(row)

    async def _delete_loaded(self, session, table, current, user, skip) -> dict:
        """Soft-delete a row whose pre-image is already loaded (fires hooks)."""
        doc = Document(table, "delete", current, current, user)
        await self._fire_pre(table, "validate", doc, skip)
        await self._fire_pre(table, "before_delete", doc, skip)
        stmt = text(
            f'UPDATE {_ident(table)} SET "_state" = {STATE_DELETED}, '
            f'"updated_by" = :u, "updated_at" = now() '
            f'WHERE "id" = :id AND "_state" != {STATE_DELETED} RETURNING *'
        )
        row = (await session.execute(stmt, {"id": current["id"], "u": user})).mappings().first()
        if row is None:
            raise NotFoundError(f"{table} {current.get('id')} not found.")
        for k, v in dict(row).items():
            doc.set(k, v)
        await self._fire_pre(table, "after_delete", doc, skip)
        return dict(row)

    async def _bulk_soft_delete(self, session, table, ids, user) -> list:
        stmt = text(
            f'UPDATE {_ident(table)} SET "_state" = {STATE_DELETED}, '
            f'"updated_by" = :u, "updated_at" = now() '
            f'WHERE "id" IN :ids AND "_state" != {STATE_DELETED} RETURNING "id"'
        ).bindparams(bindparam("ids", expanding=True))
        return (await session.execute(stmt, {"ids": ids, "u": user})).all()

    # ── hook dispatch ───────────────────────────────────────────────────
    async def _fire_pre(self, table, event, doc, skip) -> None:
        if event in skip:
            log.debug("arc.relay.hook_skipped", table=table, hook_event=event)
            return
        for fn in self._reg.hooks_for(table, event):
            res = fn(doc)
            if hasattr(res, "__await__"):
                await res

    async def _fire_on_change(self, table, event, data, previous, user) -> None:
        doc = Document(table, event, data, previous, user)
        for fn in self._reg.hooks_for(table, "on_change"):
            try:
                res = fn(doc)
                if hasattr(res, "__await__"):
                    await res
            except Exception as exc:  # isolated: one hook failing never affects others
                # NOTE: structlog reserves the keyword `event` for the log
                # message itself; passing event=... collides. Use hook_event.
                log.error("arc.relay.on_change_error", table=table,
                          hook_event=event,
                          hook=getattr(fn, "__name__", "?"), error=str(exc))

    async def _fire_tx(self, event, tx_obj) -> None:
        for fn in self._reg.tx_hooks(event):
            try:
                res = fn(tx_obj)
                if hasattr(res, "__await__"):
                    await res
            except Exception as exc:
                # See note above: structlog `event` collision.
                log.error("arc.relay.tx_hook_error", tx_event=event,
                          hook=getattr(fn, "__name__", "?"), error=str(exc))

    async def _dispatch_post(self, coro) -> None:
        """Queue a post-commit coroutine for the ASGI layer to run after the
        response is sent. Outside an HTTP request (CLI), run it inline."""
        q = _post_commit_queue.get()
        if q is None:
            try:
                await coro
            except Exception as exc:
                log.error("arc.relay.post_commit_inline_error", error=str(exc))
        else:
            q.append(coro)


__all__ = [
    "Arc", "Document", "TxContext",
    "STATE_ACTIVE", "STATE_DELETED",
    "_active_session", "_active_tx", "_post_commit_queue",
]