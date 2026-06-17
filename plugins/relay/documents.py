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

Transactions:
  • implicit — every standalone save / rm / rm_many runs in its own 1-write
    boundary. on_commit / on_rollback fire once for that boundary.
  • explicit — ``async with arc.tx() as tx:`` groups writes into ONE commit /
    rollback boundary. Per-doc ``on_change`` and global ``on_commit`` fire only
    after the real commit; a rollback discards them and fires ``on_rollback``.

``tx`` is in-memory metadata scratch only (tx.set / get / collect) — no SQL
surface — passed to on_commit(tx) / on_rollback(tx). It vanishes on rollback,
so nothing needs cleanup.

Soft delete: deleted rows carry ``_state = 99``; every method except
``arc.query`` excludes them by default (unless the caller filters on _state).
Hooks NEVER fire on read ops.
"""

from __future__ import annotations

import re
from collections import defaultdict
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError as SAIntegrityError

from arc.kernel.context import get_user
from arc.kernel.logger import get_logger

from plugins.relay.errors import (
    AmbiguousTarget, BadParam, ConflictError, IntegrityError, NotFoundError,
    ValidationError,
)
from plugins.relay.registry import Relay

log = get_logger("arc.plugin.relay.documents")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STATE_ACTIVE = 0
STATE_DELETED = 99
DEFAULT_LIST_CAP = 1000
DEFAULT_RM_MANY_CAP = 1000
_AGG_FUNCS = frozenset({"sum", "avg", "min", "max", "count"})

# Request / transaction-scoped context.
_active_session: ContextVar[Any] = ContextVar("arc_active_session", default=None)
_active_tx: ContextVar["TxContext | None"] = ContextVar("arc_active_tx", default=None)
_post_commit_queue: ContextVar["list | None"] = ContextVar("arc_post_commit", default=None)


# ── SQL helpers ──────────────────────────────────────────────────────────────

def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise BadParam(f"Illegal identifier: {name!r}")
    return f'"{name}"'


_OPS: dict[str, str] = {
    "=": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<=",
    "like": "LIKE", "ilike": "ILIKE", "in": "IN", "not_in": "NOT IN",
    "is_null": "IS NULL", "is_not_null": "IS NOT NULL",
}


def _normalize_filters(filters) -> list[tuple]:
    """Accept a dict (all-equality) or a list of 3-tuples; return a tuple list."""
    if filters is None:
        return []
    if isinstance(filters, dict):
        return [(k, "=", v) for k, v in filters.items()]
    return list(filters)


def _build_where(filters: list[tuple], params: dict, *, exclude_deleted: bool) -> str:
    """Build a WHERE body (without 'WHERE'); append the soft-delete guard unless
    the caller already filters on _state."""
    clauses: list[str] = []
    touches_state = any(f[0] == "_state" for f in filters)
    for i, (field, op, value) in enumerate(filters):
        sql_op = _OPS.get(op)
        if sql_op is None:
            raise BadParam(f"Unknown operator {op!r} for field {field!r}.")
        col = _ident(field)
        if op in ("is_null", "is_not_null"):
            clauses.append(f"{col} {sql_op}")
        elif op in ("in", "not_in"):
            if not isinstance(value, (list, tuple)) or not value:
                raise BadParam(f"{op!r} needs a non-empty list for {field!r}.")
            keys = []
            for j, v in enumerate(value):
                k = f"f{i}_{j}"
                params[k] = v
                keys.append(f":{k}")
            clauses.append(f"{col} {sql_op} ({', '.join(keys)})")
        else:
            k = f"f{i}"
            params[k] = value
            clauses.append(f"{col} {sql_op} :{k}")

    if exclude_deleted and not touches_state:
        clauses.append(f'"_state" != {STATE_DELETED}')
    return " AND ".join(clauses) if clauses else "TRUE"


def _order_clause(order: str) -> str:
    desc = order.startswith("-")
    col = order[1:] if desc else order
    return f'{_ident(col)} {"DESC" if desc else "ASC"}'


def _current_user() -> str | None:
    u = get_user()
    return getattr(u, "id", None) if u else None


def _conflict_detail(exc: Exception) -> str:
    msg = str(getattr(exc, "orig", exc)).lower()
    if "unique" in msg or "duplicate" in msg:
        return "A row with these unique values already exists."
    if "foreign key" in msg:
        return "References a row that does not exist."
    return "Constraint violation."


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _assert_read_only(sql: str) -> None:
    """Statement-level read-only guard for arc.query (not a separate txn)."""
    s = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not s:
        raise BadParam("Empty query.")
    if ";" in s:
        raise BadParam("arc.query allows a single statement only.")
    head = s.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        raise BadParam("arc.query allows read-only SELECT / WITH statements only.")


def _skip_set(local_vars: dict) -> set[str]:
    """Turn skip_* keyword flags into a set of event names."""
    return {k[len("skip_"):] for k, v in local_vars.items()
            if k.startswith("skip_") and v is True}


# ── Transaction scratch object ───────────────────────────────────────────────

class TxContext:
    """In-memory metadata carried for the lifetime of one transaction and passed
    to on_commit(tx) / on_rollback(tx). No SQL surface — it never touches the DB,
    so it disappears automatically on rollback.

    ``_pending`` holds per-doc on_change payloads accrued during the txn; the
    boundary flushes them only after a successful commit."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._collected: dict[str, list] = defaultdict(list)
        self._pending: list[tuple] = []
        self.error: Exception | None = None

    # public scratch API (metadata only)
    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def collect(self, key: str, value: Any) -> None:
        self._collected[key].append(value)

    def collected(self, key: str) -> list:
        return list(self._collected.get(key, []))

    # internal
    def _add_change(self, table, event, data, previous, user) -> None:
        self._pending.append((table, event, data, previous, user))


# ── Document handed to hooks ─────────────────────────────────────────────────

class _Old:
    """Null-object view of the prior row. ``doc.old.field`` is None on insert,
    never raises."""

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

    # field sugar
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
        self._unique_cache: dict[str, list[str]] = {}
        self.list_cap = DEFAULT_LIST_CAP
        self.rm_many_cap = DEFAULT_RM_MANY_CAP

    def _bind(self, session_cm: Callable, registrar: Relay, *,
              list_cap: int | None = None, rm_many_cap: int | None = None) -> None:
        self._cm = session_cm
        self._reg = registrar
        if list_cap:
            self.list_cap = list_cap
        if rm_many_cap:
            self.rm_many_cap = rm_many_cap

    def _ready(self) -> None:
        if self._cm is None or self._reg is None:
            raise RuntimeError("arc is not initialised — relay plugin not set up yet.")

    # ── session resolution (reads) ──────────────────────────────────────
    @asynccontextmanager
    async def _read_session(self):
        """Active txn session if present (sees uncommitted writes), else fresh."""
        self._ready()
        sess = _active_session.get()
        if sess is not None:
            yield sess
        else:
            async with self._cm() as s:
                yield s

    # ── transaction boundary (writes) ───────────────────────────────────
    @asynccontextmanager
    async def _boundary(self):
        """Yield (session, tx, owns). If an outer arc.tx() is active, join it and
        let the outer boundary own the commit + tx hooks. Otherwise open an
        implicit single-write boundary that commits and fires on_commit /
        on_rollback once."""
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

    # ── READ OPS (no hooks) ─────────────────────────────────────────────
    async def get(self, table: str, filters) -> dict | None:
        """One whole doc by any filters. Ambiguous → latest by updated_at.
        Nothing found → None."""
        rows = await self.list(table, fields=None, filters=filters,
                               order="-updated_at", limit=1)
        return rows[0] if rows else None

    async def list(self, table: str, *, fields: list[str] | None = None,
                   filters=None, order: str = "-updated_at",
                   limit: int | None = None, offset: int | None = None) -> list[dict]:
        """Matching docs. ``fields`` is required for projection (no '*'); pass
        None only for internal single-row fetches. Default sort updated_at desc.
        A safety cap applies when no explicit limit is given."""
        cols = "*"
        if fields:
            wanted = list(dict.fromkeys(["id", *fields]))   # id always included
            cols = ", ".join(_ident(c) for c in wanted)

        params: dict = {}
        where = _build_where(_normalize_filters(filters), params, exclude_deleted=True)
        sql = (f"SELECT {cols} FROM {_ident(table)} WHERE {where} "
               f"ORDER BY {_order_clause(order)}")

        capped = limit if limit is not None else self.list_cap
        sql += f" LIMIT {int(capped)}"
        if offset:
            sql += f" OFFSET {int(offset)}"

        async with self._read_session() as s:
            rows = (await s.execute(text(sql), params)).mappings().all()
        if limit is None and len(rows) >= self.list_cap:
            log.warning("arc.relay.list_capped", table=table, cap=self.list_cap)
        return [dict(r) for r in rows]

    async def exists(self, table: str, filters) -> bool:
        params: dict = {}
        where = _build_where(_normalize_filters(filters), params, exclude_deleted=True)
        sql = f"SELECT 1 FROM {_ident(table)} WHERE {where} LIMIT 1"
        async with self._read_session() as s:
            return (await s.execute(text(sql), params)).first() is not None

    async def count(self, table: str, filters=None) -> int:
        params: dict = {}
        where = _build_where(_normalize_filters(filters), params, exclude_deleted=True)
        sql = f"SELECT COUNT(*) FROM {_ident(table)} WHERE {where}"
        async with self._read_session() as s:
            return int((await s.execute(text(sql), params)).scalar() or 0)

    async def aggregate(self, table: str, *, fn: str, field: str, where=None) -> Any:
        if fn not in _AGG_FUNCS:
            raise BadParam(f"aggregate fn must be one of {sorted(_AGG_FUNCS)}, got {fn!r}.")
        params: dict = {}
        wsql = _build_where(_normalize_filters(where), params, exclude_deleted=True)
        sql = f"SELECT {fn.upper()}({_ident(field)}) FROM {_ident(table)} WHERE {wsql}"
        async with self._read_session() as s:
            return (await s.execute(text(sql), params)).scalar()

    async def query(self, sql: str, params: dict | None = None) -> list[dict]:
        """Raw READ-ONLY SELECT for joins / dashboards. Runs on the context-bound
        session (sees uncommitted writes inside a txn / pre-commit hook; committed
        state post-commit). Parameterized only. Single statement. Does NOT apply
        the _state filter — the caller owns ``WHERE _state != 99``."""
        _assert_read_only(sql)
        async with self._read_session() as s:
            rows = (await s.execute(text(sql), params or {})).mappings().all()
        return [dict(r) for r in rows]

    # ── WRITE OP (hooks fire) ───────────────────────────────────────────
    async def save(self, table: str, data: dict, *,
                   skip_validate=False, skip_before_insert=False, skip_after_insert=False,
                   skip_before_update=False, skip_after_update=False,
                   skip_before_delete=False, skip_after_delete=False,
                   skip_on_change=False) -> dict:
        """Upsert. Classify insert vs update (id → unique-key → insert) BEFORE
        writing, so the correct hook chain runs."""
        skip = _skip_set(locals())
        user = _current_user()
        async with self._boundary() as (session, tx_obj, _owns):
            old = await self._classify(session, table, data)
            event = "update" if old else "insert"
            doc = Document(table, event, data, old, user)

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

    # ── DELETE OPS (soft, hooks fire) ───────────────────────────────────
    async def rm(self, table: str, filters, *,
                 skip_validate=False, skip_before_delete=False,
                 skip_after_delete=False, skip_on_change=False) -> dict | None:
        """Soft-delete ONE doc. Ambiguous filter → raise. None found → None."""
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
                row = await self._delete_one(session, table, matches[0], user, skip)
                if "on_change" not in skip and self._reg.hooks_for(table, "on_change"):
                    tx_obj._add_change(table, "delete", row, row, user)
                result = row
        return result

    async def rm_many(self, table: str, filters, *, order: str = "-updated_at",
                      limit: int | None = None,
                      skip_validate=False, skip_before_delete=False,
                      skip_after_delete=False, skip_on_change=False) -> int:
        """Soft-delete ALL matching docs, firing per-doc hooks for each. A hard
        cap bounds the transaction."""
        skip = _skip_set(locals())
        user = _current_user()
        cap = min(limit or self.rm_many_cap, self.rm_many_cap)
        count = 0
        async with self._boundary() as (session, tx_obj, _owns):
            ids = await self._match_ids(session, table, filters, limit=cap, order=order)
            for rid in ids:
                row = await self._delete_one(session, table, rid, user, skip)
                if "on_change" not in skip and self._reg.hooks_for(table, "on_change"):
                    tx_obj._add_change(table, "delete", row, row, user)
                count += 1
        return count

    # ── classification + low-level SQL ──────────────────────────────────
    async def _classify(self, session, table: str, data: dict) -> dict | None:
        if data.get("id"):
            row = await self._fetch_by_id(session, table, data["id"])
            if row is None:
                raise NotFoundError(f"{table} {data['id']} not found.")
            return row
        for col in await self._unique_columns(session, table):
            if data.get(col) is not None:
                row = await self._fetch_one(session, table, [(col, "=", data[col])])
                if row is not None:
                    return row
                break   # first present unique key decides; no match → insert
        return None

    async def _unique_columns(self, session, table: str) -> list[str]:
        if table in self._unique_cache:
            return self._unique_cache[table]
        sql = text(
            "SELECT kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            "WHERE tc.table_name = :t AND tc.constraint_type = 'UNIQUE'"
        )
        rows = (await session.execute(sql, {"t": table})).scalars().all()
        cols = [c for c in rows if c != "id"]
        self._unique_cache[table] = cols
        return cols

    async def _fetch_by_id(self, session, table: str, row_id: str) -> dict | None:
        stmt = text(
            f'SELECT * FROM {_ident(table)} '
            f'WHERE "id" = :id AND "_state" != {STATE_DELETED} LIMIT 1'
        )
        row = (await session.execute(stmt, {"id": row_id})).mappings().first()
        return dict(row) if row else None

    async def _fetch_one(self, session, table: str, filters: list[tuple]) -> dict | None:
        params: dict = {}
        where = _build_where(filters, params, exclude_deleted=True)
        stmt = text(f"SELECT * FROM {_ident(table)} WHERE {where} "
                    f"ORDER BY {_order_clause('-updated_at')} LIMIT 1")
        row = (await session.execute(stmt, params)).mappings().first()
        return dict(row) if row else None

    async def _match_ids(self, session, table: str, filters, *, limit: int,
                         order: str = "-updated_at") -> list[str]:
        params: dict = {}
        where = _build_where(_normalize_filters(filters), params, exclude_deleted=True)
        stmt = text(f'SELECT "id" FROM {_ident(table)} WHERE {where} '
                    f"ORDER BY {_order_clause(order)} LIMIT {int(limit)}")
        return [r[0] for r in (await session.execute(stmt, params)).all()]


    async def _do_insert(self, session, table, data, user) -> dict:
        payload = {k: _coerce_value(v)
                   for k, v in data.items() if not k.startswith("_") and k != "id"}
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
            raise ConflictError(_conflict_detail(exc)) from exc
        except DBAPIError as exc:
            raise IntegrityError(_db_detail(exc)) from exc
        return dict(row)

    async def _do_update(self, session, table, row_id, data, user) -> dict:
        payload = {k: _coerce_value(v)
                   for k, v in data.items() if not k.startswith("_") and k != "id"}
        payload["updated_by"] = user
        if not payload:
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
            raise ConflictError(_conflict_detail(exc)) from exc
        except DBAPIError as exc:
            raise IntegrityError(_db_detail(exc)) from exc
        if row is None:
            raise NotFoundError(f"{table} {row_id} not found.")
        return dict(row)

    async def _delete_one(self, session, table, row_id, user, skip) -> dict:
        current = await self._fetch_by_id(session, table, row_id)
        if current is None:
            raise NotFoundError(f"{table} {row_id} not found.")
        doc = Document(table, "delete", current, current, user)
        await self._fire_pre(table, "validate", doc, skip)
        await self._fire_pre(table, "before_delete", doc, skip)
        stmt = text(
            f'UPDATE {_ident(table)} SET "_state" = {STATE_DELETED}, '
            f'"updated_by" = :u, "updated_at" = now() '
            f'WHERE "id" = :id AND "_state" != {STATE_DELETED} RETURNING *'
        )
        row = (await session.execute(stmt, {"id": row_id, "u": user})).mappings().first()
        if row is None:
            raise NotFoundError(f"{table} {row_id} not found.")
        for k, v in dict(row).items():
            doc.set(k, v)
        await self._fire_pre(table, "after_delete", doc, skip)
        return dict(row)

    # ── hook dispatch ───────────────────────────────────────────────────
    async def _fire_pre(self, table, event, doc, skip) -> None:
        if event in skip:
            log.debug("arc.relay.hook_skipped", table=table, event=event)
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
            except Exception as exc:   # isolated: one hook failing never affects others
                log.error("arc.relay.on_change_error", table=table, event=event,
                          hook=getattr(fn, "__name__", "?"), error=str(exc))

    async def _fire_tx(self, event, tx_obj) -> None:
        for fn in self._reg.tx_hooks(event):
            try:
                res = fn(tx_obj)
                if hasattr(res, "__await__"):
                    await res
            except Exception as exc:
                log.error("arc.relay.tx_hook_error", event=event,
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

def _coerce_value(value: Any) -> Any:
    """Coerce common string representations to Python types asyncpg requires.

    asyncpg is strict about types: a DATE column must receive ``datetime.date``,
    not the string ``"2024-02-01"``. This converts ISO date/datetime strings
    transparently so callers (including JSON payloads) never have to pre-convert.
    """
    import datetime as _dt

    if not isinstance(value, str):
        return value
    v = value.strip()
    # YYYY-MM-DD  →  datetime.date
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        try:
            return _dt.date.fromisoformat(v)
        except ValueError:
            pass
    # ISO datetime with T or space separator  →  datetime.datetime
    if len(v) > 10 and (v[10] in ("T", " ")):
        try:
            return _dt.datetime.fromisoformat(v)
        except ValueError:
            pass
    return value


def _db_detail(exc: Exception) -> str:
    """Extract a readable message from a raw DBAPIError."""
    orig = str(getattr(exc, "orig", exc))
    # asyncpg wraps the PG error; grab the first meaningful line
    first = orig.split("\n")[0]
    return first if len(first) < 200 else "Database rejected the row."


__all__ = ["Arc", "Document", "TxContext",
           "STATE_ACTIVE", "STATE_DELETED",
           "_active_session", "_active_tx", "_post_commit_queue"]