"""
plugins.relay.filters
======================
The single canonical operator vocabulary + the WHERE / ORDER builders + the
querystring and resource-filter compilation.

This module imports only stdlib + ``relay.errors`` — it is pure and
unit-testable without the kernel or a database.

Canonical operator tokens (used EVERYWHERE — resource declarations, internal
filter tuples, and after the ``__`` prefix is stripped from URL params):

    eq  ne  gt  gte  lt  lte  like  ilike  in  nin  null  nnull

URL form:
    ?field=value             → (field, "eq",  "value")
    ?field__gte=2024-01-01   → (field, "gte", "2024-01-01")
    ?status__in=A,B,C        → (field, "in",  ["A","B","C"])
    ?deleted_at__null        → (field, "null", None)

Resource ``static`` form (dict-of-dicts) uses the same tokens:
    {"status": {"in": ["Active","Draft"]}, "_state": {"ne": 99}}
"""

from __future__ import annotations

import re
from typing import Any

from plugins.relay.errors import BadParam

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Canonical token → SQL operator. This is the ONLY place tokens map to SQL.
CANON_OPS: dict[str, str] = {
    "eq": "=", "ne": "!=",
    "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "like": "LIKE", "ilike": "ILIKE",
    "in": "IN", "nin": "NOT IN",
    "null": "IS NULL", "nnull": "IS NOT NULL",
}
_LIST_OPS = frozenset({"in", "nin"})
_NULLARY_OPS = frozenset({"null", "nnull"})

# Querystring keys that are controls, never column filters.
RESERVED_QS = frozenset({"limit", "offset", "sort_by", "sort_order", "q"})

STATE_ACTIVE = 0
STATE_DELETED = 99


def ident(name: str) -> str:
    """Quote a validated SQL identifier. Raises BadParam on anything unsafe."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise BadParam(f"Illegal identifier: {name!r}")
    return f'"{name}"'


def _op_sql(token: str) -> str:
    sql = CANON_OPS.get(token)
    if sql is None:
        raise BadParam(f"Unknown operator {token!r}. Allowed: {sorted(CANON_OPS)}.")
    return sql


def normalize_filters(filters) -> list[tuple]:
    """Coerce any accepted filter shape into a list of (field, op_token, value).

    Accepted inputs:
      • None                         → []
      • dict (all-equality)          → [(k, "eq", v), ...]
      • dict-of-dicts (resource)     → {"status": {"in": [...]}} → [("status","in",[...])]
      • list of (field, op, value)   → passthrough (op must be a canonical token)
    """
    if filters is None:
        return []
    if isinstance(filters, dict):
        out: list[tuple] = []
        for k, v in filters.items():
            if isinstance(v, dict):
                for op, val in v.items():
                    out.append((k, op, val))
            else:
                out.append((k, "eq", v))
        return out
    return [tuple(f) for f in filters]


def build_where(filters: list[tuple], params: dict, *, exclude_deleted: bool,
                deleted_state: int = STATE_DELETED) -> str:
    """Build a WHERE body (without the 'WHERE' keyword). Appends the soft-delete
    guard unless the caller already filters on ``_state``. Values are always
    bound parameters; identifiers always pass ``ident()``."""
    clauses: list[str] = []
    touches_state = any(f[0] == "_state" for f in filters)
    for i, (field, op, value) in enumerate(filters):
        sql_op = _op_sql(op)
        col = ident(field)
        if op in _NULLARY_OPS:
            clauses.append(f"{col} {sql_op}")
        elif op in _LIST_OPS:
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
        clauses.append(f'"_state" != {int(deleted_state)}')
    return " AND ".join(clauses) if clauses else "TRUE"


def order_clause(order: str) -> str:
    """`'-updated_at'` → `"updated_at" DESC`; `'name'` → `"name" ASC`."""
    desc = order.startswith("-")
    col = order[1:] if desc else order
    return f'{ident(col)} {"DESC" if desc else "ASC"}'


def search_clause(qfields: list[str], term: str, params: dict, *, key: str = "q") -> str:
    """Build an `(f1 ILIKE :q OR f2 ILIKE :q ...)` clause for ?q= search."""
    if not qfields or not term:
        return "TRUE"
    params[key] = f"%{term}%"
    ors = " OR ".join(f"{ident(f)} ILIKE :{key}" for f in qfields)
    return f"({ors})"


# ── Resource / querystring compilation ───────────────────────────────────────

def allowed_ops_map(optional) -> dict[str, set[str]]:
    """Compile a resource ``filters.optional`` declaration into
    ``{field: {allowed op tokens}}``.

    Accepts either a bare list (all canonical ops allowed) or a field→[ops] map.
    """
    out: dict[str, set[str]] = {}
    if not optional:
        return out
    if isinstance(optional, dict):
        for field, ops in optional.items():
            out[field] = set(ops) if ops else set(CANON_OPS)
    else:
        for field in optional:
            out[field] = set(CANON_OPS)
    return out


def parse_qs(query: dict[str, str], *, allowed: dict[str, set[str]] | None = None
             ) -> tuple[list[tuple], dict]:
    """Split a querystring dict into (filters, controls).

    ``controls`` holds only the present reserved keys (limit/offset/sort_by/
    sort_order/q). ``allowed`` (if given) restricts which field+op pairs may be
    filtered; ``sort_by`` must also be an allowed field.
    """
    filters: list[tuple] = []
    controls: dict[str, Any] = {}

    for raw_key, raw_val in query.items():
        if raw_key in RESERVED_QS:
            controls[raw_key] = raw_val
            continue

        if "__" in raw_key:
            field, _, op = raw_key.partition("__")
        else:
            field, op = raw_key, "eq"

        if op not in CANON_OPS:
            raise BadParam(f"Unknown operator '__{op}' on {field!r}.")
        if allowed is not None:
            if field not in allowed:
                raise BadParam(f"Filtering on {field!r} is not allowed for this resource.")
            if op not in allowed[field]:
                raise BadParam(f"Operator '{op}' is not allowed on {field!r}.")

        if op in _LIST_OPS:
            value: Any = [p for p in raw_val.split(",") if p != ""]
        elif op in _NULLARY_OPS:
            value = None
        else:
            value = raw_val
        filters.append((field, op, value))

    if "sort_by" in controls and allowed is not None and controls["sort_by"] not in allowed:
        raise BadParam(f"Sorting on {controls['sort_by']!r} is not allowed.")
    return filters, controls


def resolve_order(controls: dict, *, default: str = "-updated_at") -> str:
    sb = controls.get("sort_by")
    if not sb:
        return default
    so = str(controls.get("sort_order") or "asc").lower()
    if so not in ("asc", "desc"):
        raise BadParam("sort_order must be 'asc' or 'desc'.")
    return f"-{sb}" if so == "desc" else sb


def resolve_limit(controls: dict, *, resource_limit: int | None, hard_cap: int) -> int:
    want = resource_limit or hard_cap
    raw = controls.get("limit")
    if raw is not None:
        try:
            want = int(raw)
        except (TypeError, ValueError):
            raise BadParam("limit must be an integer.")
    return max(1, min(want, hard_cap))


def resolve_offset(controls: dict) -> int:
    raw = controls.get("offset")
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        raise BadParam("offset must be an integer.")


__all__ = [
    "CANON_OPS", "RESERVED_QS", "STATE_ACTIVE", "STATE_DELETED",
    "ident", "normalize_filters", "build_where", "order_clause", "search_clause",
    "allowed_ops_map", "parse_qs", "resolve_order", "resolve_limit", "resolve_offset",
]