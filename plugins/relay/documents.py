"""
arc.plugins.relay.documents
===========================
Two tiers of hook dispatch around every write, plus two bypass mechanisms:

  BYPASS 1 — skip= on gateway methods
             skip specific named events, all others still fire.
             e.g. insert(..., skip={"on_insert"})  for bulk imports

  BYPASS 2 — ctx.documents.raw
             completely hook-free SQL. For migrations, seed data, system
             operations where hooks must never fire.

Filter API on DbRead / PostCommitDbRead:

  get(table, id)                      single row by primary key
  get_by(table, **eq)                 first row matching equality filters
  find(table, filters, **kw)          multiple rows, rich operators
  count(table, filters, **kw)         row count
  exists(table, **eq)                 fast existence check (equality only)
  exists_where(table, filters)        fast existence check (rich operators)

Filter operator syntax (used in find / count / exists_where):
  ("field", "=",          value)
  ("field", "!=",         value)
  ("field", ">",          value)
  ("field", ">=",         value)
  ("field", "<",          value)
  ("field", "<=",         value)
  ("field", "in",         [v1, v2])
  ("field", "not_in",     [v1, v2])
  ("field", "like",       "pattern%")
  ("field", "ilike",      "pattern%")
  ("field", "is_null",    None)
  ("field", "is_not_null",None)
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from arc.kernel.context import get_user
from arc.kernel.logger import get_logger
from plugins.relay.registry import Relay, ValidationError

log = get_logger("arc.plugin.relay.documents")

_IDENT      = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATE_ACTIVE  = 0
_STATE_TRASHED = 2

# Valid filter operators and their SQL equivalents
_OPS: dict[str, str] = {
    "=":           "=",
    "!=":          "!=",
    ">":           ">",
    ">=":          ">=",
    "<":           "<",
    "<=":          "<=",
    "like":        "LIKE",
    "ilike":       "ILIKE",
    "in":          "IN",
    "not_in":      "NOT IN",
    "is_null":     "IS NULL",
    "is_not_null": "IS NOT NULL",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ident(name: str) -> str:
    """Whitelist + double-quote a SQL identifier."""
    if not _IDENT.match(name):
        raise ValidationError(f"Illegal identifier: '{name}'")
    return f'"{name}"'


def _build_where(
    filters: list[tuple],
    params: dict,
    *,
    extra_eq: dict | None = None,
) -> str:
    """
    Build a WHERE clause from a filter list and/or equality kwargs.
    Mutates ``params`` with the bound values.
    Returns the WHERE string (without the word WHERE).

    Each filter is a 3-tuple: (field, operator, value).
    For is_null / is_not_null the value is ignored.
    For in / not_in the value must be a list/tuple.
    """
    clauses: list[str] = []
    counter = [0]

    def _key(field: str) -> str:
        counter[0] += 1
        return f"_f{counter[0]}_{field}"

    # Rich filters
    for field, op, value in (filters or []):
        op_lower = op.lower()
        if op_lower not in _OPS:
            raise ValidationError(f"Unknown filter operator: '{op}'")
        col = _ident(field)
        sql_op = _OPS[op_lower]

        if op_lower in ("is_null", "is_not_null"):
            clauses.append(f"{col} {sql_op}")

        elif op_lower in ("in", "not_in"):
            if not value:
                # IN () is invalid SQL; treat as always-false / always-true
                clauses.append("1=0" if op_lower == "in" else "1=1")
            else:
                keys = [_key(field) for _ in value]
                placeholders = ", ".join(f":{k}" for k in keys)
                for k, v in zip(keys, value):
                    params[k] = v
                clauses.append(f"{col} {sql_op} ({placeholders})")

        else:
            k = _key(field)
            params[k] = value
            clauses.append(f"{col} {sql_op} :{k}")

    # Simple equality shorthand (from **eq kwargs)
    for field, value in (extra_eq or {}).items():
        k = _key(field)
        params[k] = value
        clauses.append(f"{_ident(field)} = :{k}")

    return " AND ".join(clauses) if clauses else "1=1"


def _build_order(order_by: str | list[str] | None) -> str:
    if not order_by:
        return ""
    cols = [order_by] if isinstance(order_by, str) else order_by
    parts = []
    for col in cols:
        col = col.strip()
        if col.startswith("-"):
            parts.append(f"{_ident(col[1:])} DESC")
        else:
            parts.append(f"{_ident(col)} ASC")
    return " ORDER BY " + ", ".join(parts)


# ── Errors ────────────────────────────────────────────────────────────────────

class ConflictError(Exception):
    """Unique / FK violation -> 409."""

class NotFoundError(Exception):
    """Row does not exist or is trashed -> 404."""

class _PostCommitHookError(Exception):
    """Internal: raised by PostCommitDocument.fail() to stop that one hook."""


# ── WriteResult ───────────────────────────────────────────────────────────────

@dataclass
class WriteResult:
    data:        dict
    post_commit: Callable   # async () -> None, attached as BackgroundTask by ASGI


# ─────────────────────────────────────────────────────────────────────────────
#  Read helpers
# ─────────────────────────────────────────────────────────────────────────────

class DbRead:
    """
    Pre-commit read helper. All reads share the SAME session/transaction
    as the in-flight write, so after_insert can see the row just created.
    """

    def __init__(self, session) -> None:
        self._s = session

    # ── get ──────────────────────────────────────────────────────────
    async def get(self, table: str, row_id: str) -> dict | None:
        """Fetch a single row by primary key. Returns None if not found."""
        stmt = text(f'SELECT * FROM {_ident(table)} WHERE "id" = :id LIMIT 1')
        row  = (await self._s.execute(stmt, {"id": row_id})).first()
        return dict(row._mapping) if row else None

    # ── get_by ───────────────────────────────────────────────────────
    async def get_by(self, table: str, **eq: Any) -> dict | None:
        """
        Fetch the first row matching all keyword equality filters.
        Returns None if no match.

            emp = await doc.db.get_by("Employee", employee_code="EMP001")
        """
        if not eq:
            raise ValidationError("get_by() needs at least one filter.")
        params: dict = {}
        where  = _build_where([], params, extra_eq=eq)
        stmt   = text(f"SELECT * FROM {_ident(table)} WHERE {where} LIMIT 1")
        row    = (await self._s.execute(stmt, params)).first()
        return dict(row._mapping) if row else None

    # ── find ─────────────────────────────────────────────────────────
    async def find(
        self,
        table:    str,
        filters:  list[tuple] | None = None,
        *,
        order_by: str | list[str] | None = None,
        limit:    int | None = None,
        offset:   int | None = None,
        **eq:     Any,
    ) -> list[dict]:
        """
        Return multiple rows with rich filtering.

            rows = await doc.db.find(
                "Employee",
                [("salary", ">=", 50000), ("status", "in", ["active", "probation"])],
                order_by="-created_at",
                limit=20,
                department=dept_id,     # equality shorthand
            )
        """
        params: dict = {}
        where  = _build_where(filters or [], params, extra_eq=eq)
        order  = _build_order(order_by)
        lim    = f" LIMIT {int(limit)}"   if limit  is not None else ""
        off    = f" OFFSET {int(offset)}" if offset is not None else ""
        stmt   = text(
            f"SELECT * FROM {_ident(table)} WHERE {where}{order}{lim}{off}"
        )
        rows = (await self._s.execute(stmt, params)).all()
        return [dict(r._mapping) for r in rows]

    # ── count ────────────────────────────────────────────────────────
    async def count(
        self,
        table:   str,
        filters: list[tuple] | None = None,
        **eq:    Any,
    ) -> int:
        """
        Count rows matching filters.

            n = await doc.db.count("Employee", [("status", "=", "active")])
            n = await doc.db.count("Employee", department=dept_id)
        """
        params: dict = {}
        where  = _build_where(filters or [], params, extra_eq=eq)
        stmt   = text(f'SELECT COUNT(*) FROM {_ident(table)} WHERE {where}')
        result = (await self._s.execute(stmt, params)).scalar()
        return int(result or 0)

    # ── exists ───────────────────────────────────────────────────────
    async def exists(self, table: str, **eq: Any) -> bool:
        """
        Fast existence check using equality filters only.

            if await doc.db.exists("Department", id=dept_id): ...
        """
        if not eq:
            raise ValidationError("exists() needs at least one filter.")
        params: dict = {}
        where  = _build_where([], params, extra_eq=eq)
        stmt   = text(f"SELECT 1 FROM {_ident(table)} WHERE {where} LIMIT 1")
        return (await self._s.execute(stmt, params)).first() is not None

    # ── exists_where ─────────────────────────────────────────────────
    async def exists_where(self, table: str, filters: list[tuple]) -> bool:
        """
        Fast existence check with rich operators.

            if await doc.db.exists_where("Leave", [
                ("employee", "=", emp_id),
                ("status",   "in", ["pending", "approved"]),
                ("from_date","<=", today),
                ("to_date",  ">=", today),
            ]): ...
        """
        if not filters:
            raise ValidationError("exists_where() needs at least one filter.")
        params: dict = {}
        where  = _build_where(filters, params)
        stmt   = text(f"SELECT 1 FROM {_ident(table)} WHERE {where} LIMIT 1")
        return (await self._s.execute(stmt, params)).first() is not None


# ── PostCommitDbRead ──────────────────────────────────────────────────────────

class PostCommitDbRead:
    """
    Post-commit read helper. The committed transaction is gone — each method
    opens and closes its OWN fresh session so no connection is held open
    across the async work of multiple hooks.
    Same API as DbRead.
    """

    def __init__(self, session_cm: Callable) -> None:
        self._cm = session_cm

    async def get(self, table: str, row_id: str) -> dict | None:
        async with self._cm() as s:
            stmt = text(f'SELECT * FROM {_ident(table)} WHERE "id" = :id LIMIT 1')
            row  = (await s.execute(stmt, {"id": row_id})).first()
            return dict(row._mapping) if row else None

    async def get_by(self, table: str, **eq: Any) -> dict | None:
        if not eq:
            raise ValidationError("get_by() needs at least one filter.")
        async with self._cm() as s:
            params: dict = {}
            where = _build_where([], params, extra_eq=eq)
            stmt  = text(f"SELECT * FROM {_ident(table)} WHERE {where} LIMIT 1")
            row   = (await s.execute(stmt, params)).first()
            return dict(row._mapping) if row else None

    async def find(
        self,
        table:    str,
        filters:  list[tuple] | None = None,
        *,
        order_by: str | list[str] | None = None,
        limit:    int | None = None,
        offset:   int | None = None,
        **eq:     Any,
    ) -> list[dict]:
        async with self._cm() as s:
            params: dict = {}
            where  = _build_where(filters or [], params, extra_eq=eq)
            order  = _build_order(order_by)
            lim    = f" LIMIT {int(limit)}"   if limit  is not None else ""
            off    = f" OFFSET {int(offset)}" if offset is not None else ""
            stmt   = text(
                f"SELECT * FROM {_ident(table)} WHERE {where}{order}{lim}{off}"
            )
            rows = (await s.execute(stmt, params)).all()
            return [dict(r._mapping) for r in rows]

    async def count(
        self,
        table:   str,
        filters: list[tuple] | None = None,
        **eq:    Any,
    ) -> int:
        async with self._cm() as s:
            params: dict = {}
            where  = _build_where(filters or [], params, extra_eq=eq)
            stmt   = text(f'SELECT COUNT(*) FROM {_ident(table)} WHERE {where}')
            result = (await s.execute(stmt, params)).scalar()
            return int(result or 0)

    async def exists(self, table: str, **eq: Any) -> bool:
        if not eq:
            raise ValidationError("exists() needs at least one filter.")
        async with self._cm() as s:
            params: dict = {}
            where  = _build_where([], params, extra_eq=eq)
            stmt   = text(f"SELECT 1 FROM {_ident(table)} WHERE {where} LIMIT 1")
            return (await s.execute(stmt, params)).first() is not None

    async def exists_where(self, table: str, filters: list[tuple]) -> bool:
        if not filters:
            raise ValidationError("exists_where() needs at least one filter.")
        async with self._cm() as s:
            params: dict = {}
            where  = _build_where(filters, params)
            stmt   = text(f"SELECT 1 FROM {_ident(table)} WHERE {where} LIMIT 1")
            return (await s.execute(stmt, params)).first() is not None


# ─────────────────────────────────────────────────────────────────────────────
#  Document objects
# ─────────────────────────────────────────────────────────────────────────────

class Document:
    """Handed to pre-commit hooks. fail() -> rollback -> 422."""

    def __init__(
        self, table: str, event: str, data: dict,
        db: DbRead, user: str | None,
    ) -> None:
        self.table    = table
        self.event    = event
        self.data:    dict = dict(data)
        self.previous: dict | None = None
        self.id:      str | None = None
        self.user     = user
        self.db       = db

    def get(self, field: str, default: Any = None) -> Any:
        return self.data.get(field, default)

    def set(self, field: str, value: Any) -> None:
        self.data[field] = value

    def fail(self, message: str, *, field: str | None = None) -> None:
        raise ValidationError(message, field=field)

    def _bind_row(self, row: dict) -> None:
        self.data = dict(row)
        self.id   = row.get("id")


class PostCommitDocument:
    """Handed to on_insert / on_update / on_delete. fail() logs only."""

    def __init__(
        self, table: str, event: str, data: dict,
        db: PostCommitDbRead, user: str | None,
        previous: dict | None = None,
    ) -> None:
        self.table    = table
        self.event    = event
        self.data:    dict = dict(data)
        self.previous = previous
        self.id:      str | None = data.get("id")
        self.user     = user
        self.db       = db

    def get(self, field: str, default: Any = None) -> Any:
        return self.data.get(field, default)

    def fail(self, message: str, *, field: str | None = None) -> None:
        log.error(
            "arc.relay.post_commit_fail",
            table=self.table, event=self.event,
            message=message, field=field,
        )
        raise _PostCommitHookError(message)


# ─────────────────────────────────────────────────────────────────────────────
#  RawGateway — BYPASS 2: no hooks at all
# ─────────────────────────────────────────────────────────────────────────────

class RawGateway:
    """
    Direct SQL writes with NO hooks — pre-commit or post-commit.
    Use only for:
      * data migrations / seed scripts
      * system-level corrections
      * bulk imports where hook overhead is intentionally skipped

    Returns plain dicts (no WriteResult / no BackgroundTask).
    Access via: ctx.documents.raw.insert(...)
    """

    def __init__(self, session_cm: Callable) -> None:
        self._cm = session_cm

    async def insert(self, table: str, data: dict, *, user: str | None = None) -> dict:
        user = user or _current_user()
        async with self._cm() as s:
            try:
                payload = {
                    k: v for k, v in data.items()
                    if not k.startswith("_") and k != "id"
                }
                payload.setdefault("created_by", user)
                payload.setdefault("updated_by", user)
                cols  = ", ".join(_ident(k) for k in payload)
                binds = ", ".join(f":{k}" for k in payload)
                stmt  = text(
                    f"INSERT INTO {_ident(table)} ({cols}) VALUES ({binds}) RETURNING *"
                )
                row = (await s.execute(stmt, payload)).first()
                await s.commit()
                return dict(row._mapping)
            except IntegrityError as exc:
                await s.rollback()
                raise ConflictError(_conflict_detail(exc)) from exc
            except Exception:
                await s.rollback()
                raise

    async def update(
        self, table: str, row_id: str, data: dict, *, user: str | None = None
    ) -> dict:
        user = user or _current_user()
        async with self._cm() as s:
            try:
                payload = {
                    k: v for k, v in data.items()
                    if not k.startswith("_") and k != "id"
                }
                payload["updated_by"] = user
                sets   = ", ".join(f"{_ident(k)} = :{k}" for k in payload)
                params = {**payload, "id": row_id}
                stmt   = text(
                    f'UPDATE {_ident(table)} SET {sets}, "updated_at" = now() '
                    f'WHERE "id" = :id AND "_state" = {_STATE_ACTIVE} RETURNING *'
                )
                try:
                    row = (await s.execute(stmt, params)).first()
                except IntegrityError as exc:
                    raise ConflictError(_conflict_detail(exc)) from exc
                if row is None:
                    raise NotFoundError(f"{table} {row_id} not found.")
                await s.commit()
                return dict(row._mapping)
            except Exception:
                await s.rollback()
                raise

    async def delete(self, table: str, row_id: str, *, user: str | None = None) -> dict:
        user = user or _current_user()
        async with self._cm() as s:
            try:
                stmt = text(
                    f'UPDATE {_ident(table)} '
                    f'SET "_state" = {_STATE_TRASHED}, "updated_by" = :u, "updated_at" = now() '
                    f'WHERE "id" = :id AND "_state" = {_STATE_ACTIVE} RETURNING *'
                )
                row = (await s.execute(stmt, {"id": row_id, "u": user})).first()
                if row is None:
                    raise NotFoundError(f"{table} {row_id} not found.")
                await s.commit()
                return dict(row._mapping)
            except Exception:
                await s.rollback()
                raise


# ─────────────────────────────────────────────────────────────────────────────
#  DocumentGateway
# ─────────────────────────────────────────────────────────────────────────────

_ALL_POST = frozenset({"on_insert", "on_update", "on_delete"})
_NOOP = lambda: None  # placeholder for skipped post-commit


async def _noop_async() -> None:
    pass


class DocumentGateway:
    """
    Transactional write pipeline.

    skip= parameter (BYPASS 1)
    ──────────────────────────
    Pass a set of event names to suppress. All other events still fire.

        # skip post-commit notifications during a bulk import
        await ctx.documents.insert("Employee", row, skip={"on_insert"})

        # skip validation for a system-generated correction
        await ctx.documents.update("Employee", id, data, skip={"validate"})

        # skip ALL post-commit hooks
        await ctx.documents.insert("Employee", row, skip=_ALL_POST)

    raw property (BYPASS 2)
    ───────────────────────
    Completely hook-free writes. No pre-commit, no post-commit.

        row = await ctx.documents.raw.insert("Employee", data)
    """

    def __init__(self, session_cm: Callable, registrar: Relay) -> None:
        self._cm  = session_cm
        self._reg = registrar
        self._raw: RawGateway | None = None

    @property
    def raw(self) -> RawGateway:
        """BYPASS 2: hook-free direct SQL writes."""
        if self._raw is None:
            self._raw = RawGateway(self._cm)
        return self._raw

    # ── Public write API ──────────────────────────────────────────────

    async def insert(
        self,
        table: str,
        data:  dict,
        *,
        skip:  set[str] | None = None,
    ) -> WriteResult:
        skip = skip or set()
        user = _current_user()

        async with self._cm() as session:
            try:
                doc = Document(table, "validate", data, DbRead(session), user)
                await self._fire_pre(table, "validate",      doc, skip)
                doc.event = "before_insert"
                await self._fire_pre(table, "before_insert", doc, skip)
                row = await self._do_insert(session, table, doc.data, user)
                doc._bind_row(row)
                doc.event = "after_insert"
                await self._fire_pre(table, "after_insert",  doc, skip)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        committed = dict(doc.data)

        async def _post() -> None:
            await self._fire_post(table, "on_insert", committed, None, user, skip)

        return WriteResult(
            data=committed,
            post_commit=_post if "on_insert" not in skip else _noop_async,
        )

    async def update(
        self,
        table:  str,
        row_id: str,
        data:   dict,
        *,
        skip:   set[str] | None = None,
    ) -> WriteResult:
        skip = skip or set()
        user = _current_user()

        async with self._cm() as session:
            try:
                current = await DbRead(session).get(table, row_id)
                if current is None or current.get("_state") == _STATE_TRASHED:
                    raise NotFoundError(f"{table} {row_id} not found.")

                doc          = Document(table, "validate", data, DbRead(session), user)
                doc.id       = row_id
                doc.previous = current
                await self._fire_pre(table, "validate",      doc, skip)
                doc.event = "before_update"
                await self._fire_pre(table, "before_update", doc, skip)
                row = await self._do_update(session, table, row_id, doc.data, user)
                doc._bind_row(row)
                doc.event = "after_update"
                await self._fire_pre(table, "after_update",  doc, skip)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        committed = dict(doc.data)
        previous  = dict(current)

        async def _post() -> None:
            await self._fire_post(table, "on_update", committed, previous, user, skip)

        return WriteResult(
            data=committed,
            post_commit=_post if "on_update" not in skip else _noop_async,
        )

    async def delete(
        self,
        table:  str,
        row_id: str,
        *,
        skip:   set[str] | None = None,
    ) -> WriteResult:
        skip = skip or set()
        user = _current_user()

        async with self._cm() as session:
            try:
                current = await DbRead(session).get(table, row_id)
                if current is None or current.get("_state") == _STATE_TRASHED:
                    raise NotFoundError(f"{table} {row_id} not found.")

                doc          = Document(table, "before_delete", current, DbRead(session), user)
                doc.id       = row_id
                doc.previous = current
                await self._fire_pre(table, "before_delete", doc, skip)
                row = await self._do_soft_delete(session, table, row_id, user)
                doc._bind_row(row)
                doc.event = "after_delete"
                await self._fire_pre(table, "after_delete",  doc, skip)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        committed = dict(doc.data)
        previous  = dict(current)

        async def _post() -> None:
            await self._fire_post(table, "on_delete", committed, previous, user, skip)

        return WriteResult(
            data=committed,
            post_commit=_post if "on_delete" not in skip else _noop_async,
        )

    # ── Hook dispatchers ──────────────────────────────────────────────

    async def _fire_pre(
        self, table: str, event: str, doc: Document, skip: set[str]
    ) -> None:
        if event in skip:
            log.debug("arc.relay.hook_skipped", table=table, event=event)
            return
        for fn in self._reg.hooks_for(table, event):
            result = fn(doc)
            if inspect.isawaitable(result):
                await result

    async def _fire_post(
        self,
        table:    str,
        event:    str,
        data:     dict,
        previous: dict | None,
        user:     str | None,
        skip:     set[str],
    ) -> None:
        if event in skip:
            log.debug("arc.relay.hook_skipped", table=table, event=event)
            return
        hooks = self._reg.hooks_for(table, event)
        if not hooks:
            return
        pc_db  = PostCommitDbRead(self._cm)
        pc_doc = PostCommitDocument(
            table=table, event=event, data=data,
            db=pc_db, user=user, previous=previous,
        )
        for fn in hooks:
            try:
                result = fn(pc_doc)
                if inspect.isawaitable(result):
                    await result
            except _PostCommitHookError:
                pass
            except Exception as exc:
                log.error(
                    "arc.relay.post_commit_hook_error",
                    table=table, event=event,
                    hook=getattr(fn, "__name__", "?"),
                    error=str(exc),
                )

    # ── SQL ───────────────────────────────────────────────────────────

    async def _do_insert(self, session, table, data, user) -> dict:
        payload = {k: v for k, v in data.items() if not k.startswith("_") and k != "id"}
        payload.setdefault("created_by", user)
        payload.setdefault("updated_by", user)
        cols  = ", ".join(_ident(k) for k in payload)
        binds = ", ".join(f":{k}" for k in payload)
        stmt  = text(
            f"INSERT INTO {_ident(table)} ({cols}) VALUES ({binds}) RETURNING *"
        )
        try:
            row = (await session.execute(stmt, payload)).first()
        except IntegrityError as exc:
            raise ConflictError(_conflict_detail(exc)) from exc
        except DBAPIError as exc:
            raise ConflictError("Database rejected the row.") from exc
        return dict(row._mapping)

    async def _do_update(self, session, table, row_id, data, user) -> dict:
        payload = {k: v for k, v in data.items() if not k.startswith("_") and k != "id"}
        payload["updated_by"] = user
        sets   = ", ".join(f"{_ident(k)} = :{k}" for k in payload)
        params = {**payload, "id": row_id}
        stmt   = text(
            f'UPDATE {_ident(table)} SET {sets}, "updated_at" = now() '
            f'WHERE "id" = :id AND "_state" = {_STATE_ACTIVE} RETURNING *'
        )
        try:
            row = (await session.execute(stmt, params)).first()
        except IntegrityError as exc:
            raise ConflictError(_conflict_detail(exc)) from exc
        if row is None:
            raise NotFoundError(f"{table} {row_id} not found.")
        return dict(row._mapping)

    async def _do_soft_delete(self, session, table, row_id, user) -> dict:
        stmt = text(
            f'UPDATE {_ident(table)} SET "_state" = {_STATE_TRASHED}, '
            f'"updated_by" = :u, "updated_at" = now() '
            f'WHERE "id" = :id AND "_state" = {_STATE_ACTIVE} RETURNING *'
        )
        row = (await session.execute(stmt, {"id": row_id, "u": user})).first()
        if row is None:
            raise NotFoundError(f"{table} {row_id} not found.")
        return dict(row._mapping)


# ── Helpers ───────────────────────────────────────────────────────────────────

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