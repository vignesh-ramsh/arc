"""
arc.plugins.psqldb.backup
====================
Backup, restore, recover, and cleanup for Arc-managed tables.

Formats
-------
JSON (default) — a structured document: per-table column metadata (with pg
    cast types) plus rows. Enables PARTIAL restore: only columns present in the
    current table are restored; missing tables are skipped with a warning.

SQL — INSERT statements with a machine-readable ``-- ARC-BACKUP-META:`` header.
    Restore is STRICT: every table's column set must match the live schema
    exactly, otherwise it fails with "schema mismatch — Restore Failed".

Encryption
----------
If enabled, the serialized bytes (json or sql) are wrapped with Fernet using a
key derived from a passphrase (PBKDF2-HMAC-SHA256, random salt stored in the
file header). Passphrase comes from --key or the env var named in
``[plugins.db.backup] encryption_key_env``.

Convert
-------
``sql_to_json()`` parses an Arc-generated SQL backup back into the JSON format
so a strict SQL backup can be replayed partially.

Hardening in this revision
--------------------------
* SQL string literals escape newlines/CR/backslash (``E'...'`` form), so every
  INSERT is guaranteed single-line. Previously a text value containing a
  literal newline produced an INSERT spanning multiple lines, and the
  line-based ``restore_sql`` executed broken fragments.
* ``restore_json`` batches inserts (executemany in chunks) instead of one
  awaited round-trip per row — large restores are orders of magnitude faster.
* ``read_backup`` streams rows with server-side cursors instead of buffering
  the full result twice.
* Value coercion keeps booleans/None intact and serializes everything else
  through one canonical path shared by both formats.
* ``backup_to_file`` (used by ``arc db backup``) streams rows straight to
  disk in row batches — peak memory is one batch, not the dataset. The
  in-memory ``read_backup`` + ``serialize_*`` path remains for tests and for
  programmatic use on small datasets.
* Encryption note: Fernet authenticates whole messages and cannot stream, so
  an encrypted backup is produced by encrypting the finished plaintext file —
  peak memory is the serialized byte size (still far below the old
  row-object-graph ceiling).
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arc.kernel.exceptions import ArcError
from arc.kernel.logger import get_logger

log = get_logger(__name__)

BACKUP_VERSION = "1.0"
ENC_MAGIC = b"ARCENC1\n"
RESTORE_BATCH_SIZE = 500

# System columns and their pg cast types (always present, always restored).
SYSTEM_PG = {
    "id": "uuid",
    "created_at": "timestamptz",
    "updated_at": "timestamptz",
    "created_by": "text",
    "updated_by": "text",
    "_state": "integer",
}

# Arc field type -> pg cast type (for user fields).
ARC_PG = {
    "Data": "varchar",
    "Text": "text",
    "Int": "integer",
    "Float": "double precision",
    "Decimal": "numeric",
    "Bool": "boolean",
    "Date": "date",
    "Datetime": "timestamptz",
    "JSON": "jsonb",
    "Link": "uuid",
}


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class TableData:
    table: str
    plugin: str
    columns: list[dict[str, str]]   # [{"name": ..., "pg": ...}, ...]
    rows: list[dict[str, Any]]


@dataclass
class Backup:
    meta: dict[str, Any]
    tables: list[TableData] = field(default_factory=list)


# ── Value coercion ───────────────────────────────────────────────────────────
def _to_jsonable(value: Any) -> Any:
    """Make a DB value JSON-serializable without losing information."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)  # string, not float — NUMERIC must round-trip
    if isinstance(value, (uuid.UUID, _dt.datetime, _dt.date, _dt.time)):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (dict, list)):
        return value
    return str(value)


# ── Read ─────────────────────────────────────────────────────────────────────
async def read_backup(conn, plugins: list[str] | None) -> Backup:
    from sqlalchemy import text

    rows = await conn.execute(text(
        "SELECT table_name, plugin, fld_id, field_name, type FROM _field_registry"
    ))
    reg: dict[str, dict[str, str]] = {}   # table -> {field_name: pg}
    table_plugin: dict[str, str] = {}
    for table_name, plugin, _fid, field_name, type_ in rows:
        reg.setdefault(table_name, {})[field_name] = ARC_PG.get(type_, "text")
        table_plugin[table_name] = plugin

    wanted = set(plugins) if plugins else None
    targets = [t for t, pl in table_plugin.items() if wanted is None or pl in wanted]
    targets.sort()

    backup = Backup(meta={
        "arc_backup_version": BACKUP_VERSION,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "plugins": sorted(wanted) if wanted else "all",
    })

    for table in targets:
        # Server-side cursor: rows arrive in chunks instead of one giant
        # buffered result set held twice in memory.
        result = await conn.stream(
            text(f'SELECT * FROM "{table}"'),
            execution_options={"stream_results": True, "yield_per": 1000},
        )
        col_names = list(result.keys())
        columns = []
        for name in col_names:
            pg = SYSTEM_PG.get(name) or reg.get(table, {}).get(name, "text")
            columns.append({"name": name, "pg": pg})
        data_rows: list[dict[str, Any]] = []
        async for row in result:
            data_rows.append(
                {name: _to_jsonable(val) for name, val in zip(col_names, row)}
            )
        backup.tables.append(TableData(table, table_plugin[table], columns, data_rows))
        log.info("arc.db.backup_table", table=table, rows=len(data_rows))

    return backup


# ── Serialize ────────────────────────────────────────────────────────────────
def serialize_json(backup: Backup) -> bytes:
    doc = {
        **backup.meta,
        "format": "json",
        "tables": [
            {"table": t.table, "plugin": t.plugin, "columns": t.columns, "rows": t.rows}
            for t in backup.tables
        ],
    }
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sql_literal(value: Any, pg: str) -> str:
    """One SQL literal, guaranteed to contain NO raw newlines.

    Strings with control characters or backslashes are emitted as ``E'...'``
    escape-string literals so every INSERT stays on a single physical line —
    the invariant restore_sql's line-based executor depends on.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if pg == "jsonb" and isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    s = str(value)
    if any(ch in s for ch in ("\\", "\n", "\r", "\t", "\0")):
        body = (
            s.replace("\\", "\\\\")
             .replace("'", "''")
             .replace("\n", "\\n")
             .replace("\r", "\\r")
             .replace("\t", "\\t")
             .replace("\0", "")
        )
        lit = f"E'{body}'"
    else:
        lit = "'" + s.replace("'", "''") + "'"
    if pg == "jsonb":
        lit += "::jsonb"
    return lit


def serialize_sql(backup: Backup) -> bytes:
    meta = {
        "format": "sql",
        "arc_backup_version": BACKUP_VERSION,
        "created_at": backup.meta.get("created_at"),
        "tables": {t.table: [c["name"] for c in t.columns] for t in backup.tables},
    }
    lines = [f"-- ARC-BACKUP-META: {json.dumps(meta)}"]
    for t in backup.tables:
        col_pg = {c["name"]: c["pg"] for c in t.columns}
        col_list = ", ".join(f'"{c["name"]}"' for c in t.columns)
        lines.append(f"\n-- table: {t.table} ({len(t.rows)} rows)")
        for row in t.rows:
            vals = ", ".join(_sql_literal(row.get(c["name"]), col_pg[c["name"]]) for c in t.columns)
            lines.append(
                f'INSERT INTO "{t.table}" ({col_list}) VALUES ({vals}) '
                f'ON CONFLICT ("id") DO NOTHING;'
            )
    return ("\n".join(lines) + "\n").encode("utf-8")


# ── Encryption ───────────────────────────────────────────────────────────────
def _derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    import os
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(data)
    return ENC_MAGIC + base64.b64encode(salt) + b"\n" + token


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken

    if not blob.startswith(ENC_MAGIC):
        raise ArcError("Not an Arc-encrypted backup.", code="arc.db.backup.not_encrypted")
    rest = blob[len(ENC_MAGIC):]
    salt_b64, _, token = rest.partition(b"\n")
    salt = base64.b64decode(salt_b64)
    try:
        return Fernet(_derive_key(passphrase, salt)).decrypt(token)
    except InvalidToken as exc:
        raise ArcError("Decryption failed — wrong key?", code="arc.db.backup.bad_key") from exc


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(ENC_MAGIC)


# ── Parse ────────────────────────────────────────────────────────────────────
def parse_json(raw: bytes) -> Backup:
    doc = json.loads(raw.decode("utf-8"))
    tables = [
        TableData(t["table"], t.get("plugin", ""), t["columns"], t["rows"])
        for t in doc.get("tables", [])
    ]
    return Backup(meta=doc, tables=tables)


_META_RE = re.compile(r"^-- ARC-BACKUP-META:\s*(\{.*\})\s*$", re.MULTILINE)


def parse_sql_meta(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8")
    m = _META_RE.search(text)
    if not m:
        raise ArcError(
            "SQL backup is missing its ARC-BACKUP-META header — cannot validate.",
            code="arc.db.backup.no_meta",
        )
    return json.loads(m.group(1))


# ── Restore ──────────────────────────────────────────────────────────────────
async def _live_columns(conn, table: str) -> set[str]:
    from sqlalchemy import text

    rows = await conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=:t"
    ), {"t": table})
    return {r[0] for r in rows}


def _bind_value(v: Any, pg: str) -> Any:
    """Coerce a JSON-backup value to a bind parameter for CAST(:x AS pg)."""
    if v is None:
        return None
    if pg == "jsonb" and not isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    return str(v)


async def restore_json(conn, backup: Backup) -> dict[str, int]:
    """Partial restore: only columns that currently exist; skip missing tables.

    Rows are inserted in batches (executemany) — previously one awaited
    round-trip per row made large restores pathologically slow.
    """
    from sqlalchemy import text

    inserted = 0
    skipped_tables = 0
    for t in backup.tables:
        live = await _live_columns(conn, t.table)
        if not live:
            log.warning("arc.db.restore_skip_table", table=t.table)
            skipped_tables += 1
            continue
        usable = [c for c in t.columns if c["name"] in live]
        names = [c["name"] for c in usable]
        if "id" not in names:
            log.warning("arc.db.restore_skip_no_id", table=t.table)
            continue
        col_list = ", ".join(f'"{n}"' for n in names)
        casts = ", ".join(f'CAST(:{n} AS {c["pg"]})' for n, c in zip(names, usable))
        sql = text(
            f'INSERT INTO "{t.table}" ({col_list}) VALUES ({casts}) '
            f'ON CONFLICT ("id") DO NOTHING'
        )
        batch: list[dict[str, Any]] = []
        for row in t.rows:
            batch.append({c["name"]: _bind_value(row.get(c["name"]), c["pg"]) for c in usable})
            if len(batch) >= RESTORE_BATCH_SIZE:
                await conn.execute(sql, batch)
                inserted += len(batch)
                batch = []
        if batch:
            await conn.execute(sql, batch)
            inserted += len(batch)
        log.info("arc.db.restore_table", table=t.table, rows=len(t.rows))
    return {"inserted": inserted, "skipped_tables": skipped_tables}


async def restore_sql(conn, raw_sql: bytes) -> dict[str, int]:
    """Strict restore: validate every table's columns match, then execute.

    Line-based execution is safe because serialize_sql guarantees one
    statement per line (string literals escape all newlines).
    """
    from sqlalchemy import text

    meta = parse_sql_meta(raw_sql)
    backup_tables: dict[str, list[str]] = meta.get("tables", {})

    # Schema validation — exact column-set match per table.
    for table, cols in backup_tables.items():
        live = await _live_columns(conn, table)
        if not live:
            raise ArcError(
                f"schema mismatch — table '{table}' does not exist. Restore Failed.",
                code="arc.db.restore.schema_mismatch",
            )
        if set(cols) != live:
            missing = set(cols) - live
            extra = live - set(cols)
            raise ArcError(
                f"schema mismatch on '{table}' (missing={sorted(missing)}, "
                f"unexpected={sorted(extra)}). Restore Failed. "
                f"Use a JSON backup for partial restore.",
                code="arc.db.restore.schema_mismatch",
            )

    # Execute INSERT statements (skip comments/blank lines).
    executed = 0
    for line in raw_sql.decode("utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        await conn.execute(text(s.rstrip(";")))
        executed += 1
    return {"executed": executed}


# ── Recover from _trash ──────────────────────────────────────────────────────
async def recover_row(conn, trash_id: int) -> str:
    from sqlalchemy import text

    row = (await conn.execute(text(
        "SELECT table_name, data, drop_type, restored_at FROM _trash WHERE id=:i"
    ), {"i": trash_id})).first()
    if row is None:
        raise ArcError(f"_trash id {trash_id} not found.", code="arc.db.recover.not_found")
    table, data, drop_type, restored_at = row
    if drop_type != "row":
        raise ArcError(f"_trash id {trash_id} is a '{drop_type}' drop, not a row.",
                       code="arc.db.recover.wrong_type")
    if isinstance(data, str):
        data = json.loads(data)
    cols = list(data.keys())
    col_list = ", ".join(f'"{c}"' for c in cols)
    binds = ", ".join(f":{c}" for c in cols)
    await conn.execute(
        text(f'INSERT INTO "{table}" ({col_list}) VALUES ({binds}) ON CONFLICT ("id") DO NOTHING'),
        {c: (json.dumps(data[c]) if isinstance(data[c], (dict, list)) else data[c]) for c in cols},
    )
    await conn.execute(text("UPDATE _trash SET restored_at=now() WHERE id=:i"), {"i": trash_id})

    # Link-target check — warn (don't fail) if the recovered row references
    # parents that are missing or soft-deleted. The operator can then
    # recover those parents too if they want a clean graph.
    warnings = await _check_recovered_links(conn, table, data)
    for w in warnings:
        log.warning("arc.db.recover.dangling_link", **w)

    return table


async def _check_recovered_links(conn, table: str, data: dict) -> list[dict]:
    """For every Link-type field on *table*, check whether the value in
    *data* points at an active row in link_table. Returns a list of
    {field, link_table, value, reason} for each problem (missing /
    soft-deleted)."""
    from sqlalchemy import text

    problems: list[dict] = []
    links = (await conn.execute(text(
        "SELECT field_name, link_table FROM _field_registry "
        "WHERE table_name = :t AND type = 'Link' "
        "AND is_virtual = false AND link_table IS NOT NULL"
    ), {"t": table})).all()
    for field_name, link_table in links:
        val = data.get(field_name)
        if val is None:
            continue
        row = (await conn.execute(text(
            f'SELECT "_state" FROM "{link_table}" WHERE "id" = :v'
        ), {"v": val})).first()
        if row is None:
            problems.append({"table": table, "field": field_name,
                             "link_table": link_table, "value": str(val),
                             "reason": "missing"})
        elif row[0] == 99:
            problems.append({"table": table, "field": field_name,
                             "link_table": link_table, "value": str(val),
                             "reason": "soft_deleted"})
    return problems


async def recover_column(conn, trash_id: int) -> str:
    from sqlalchemy import text

    row = (await conn.execute(text(
        "SELECT data, drop_type FROM _trash WHERE id=:i"
    ), {"i": trash_id})).first()
    if row is None:
        raise ArcError(f"_trash id {trash_id} not found.", code="arc.db.recover.not_found")
    data, drop_type = row
    if drop_type != "column":
        raise ArcError(f"_trash id {trash_id} is a '{drop_type}' drop, not a column.",
                       code="arc.db.recover.wrong_type")
    if isinstance(data, str):
        data = json.loads(data)

    table = data["table"]
    column = data["column"]
    arc_type = data.get("type", "Data")
    reqd = bool(data.get("reqd"))
    max_length = data.get("max_length")
    plugin = data.get("plugin", "")
    fld_id = data.get("fld_id", "")

    from plugins.psqldb.migrations.schema import render_column_type
    col_type = render_column_type(arc_type, max_length)

    # Re-add the column (nullable first; values backfilled next).
    await conn.execute(text(
        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {col_type} NULL'
    ))
    pg = ARC_PG.get(arc_type, "text")
    update_sql = text(
        f'UPDATE "{table}" SET "{column}" = CAST(:v AS {pg}) WHERE id = CAST(:rid AS uuid)'
    )
    batch: list[dict[str, Any]] = []
    for item in data.get("values", []):
        rid = item.get("id")
        val = item.get("value")
        if rid is None:
            continue
        if pg == "jsonb" and val is not None and not isinstance(val, str):
            val = json.dumps(val)
        batch.append({
            "v": None if val is None else (val if isinstance(val, str) else str(val)),
            "rid": str(rid),
        })
        if len(batch) >= RESTORE_BATCH_SIZE:
            await conn.execute(update_sql, batch)
            batch = []
    if batch:
        await conn.execute(update_sql, batch)
    if reqd:
        await conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET NOT NULL'))

    # Re-register in _field_registry.
    await conn.execute(text(
        "INSERT INTO _field_registry (fld_id, table_name, field_name, type, reqd, max_length, plugin) "
        "VALUES (:fid, :t, :c, :ty, :rq, :ml, :pl) "
        "ON CONFLICT (fld_id, table_name) DO UPDATE SET "
        "field_name=EXCLUDED.field_name, type=EXCLUDED.type, reqd=EXCLUDED.reqd, "
        "max_length=EXCLUDED.max_length, updated_at=now()"
    ), {"fid": fld_id, "t": table, "c": column, "ty": arc_type, "rq": reqd,
        "ml": max_length, "pl": plugin})

    await conn.execute(text("UPDATE _trash SET restored_at=now() WHERE id=:i"), {"i": trash_id})
    return f"{table}.{column}"


# ── Cleanup (purge old trash) ────────────────────────────────────────────────
async def cleanup_trash(conn, before: _dt.datetime, plugins: list[str] | None,
                        dry_run: bool) -> int:
    from sqlalchemy import text

    table_filter = ""
    params: dict[str, Any] = {"before": before}
    if plugins:
        rows = await conn.execute(text(
            "SELECT DISTINCT table_name FROM _field_registry WHERE plugin = ANY(:pl)"
        ), {"pl": plugins})
        tables = [r[0] for r in rows]
        if not tables:
            return 0
        table_filter = " AND table_name = ANY(:tables)"
        params["tables"] = tables

    count_sql = text(f"SELECT count(*) FROM _trash WHERE deleted_at < :before{table_filter}")
    n = (await conn.execute(count_sql, params)).scalar() or 0
    if dry_run or n == 0:
        return int(n)
    await conn.execute(text(f"DELETE FROM _trash WHERE deleted_at < :before{table_filter}"), params)
    return int(n)


# ── Convert SQL backup → JSON ────────────────────────────────────────────────
_INSERT_RE = re.compile(
    r'INSERT INTO "(?P<table>[^"]+)" \((?P<cols>[^)]*)\) VALUES \((?P<vals>.*)\) '
    r'ON CONFLICT'
)


def _split_sql_tuple(s: str) -> list[str]:
    """Split a VALUES tuple on top-level commas, respecting quoted strings."""
    out, buf, in_str, i = [], [], False, 0
    while i < len(s):
        ch = s[i]
        if ch == "'":
            if in_str and i + 1 < len(s) and s[i + 1] == "'":
                buf.append("''"); i += 2; continue
            in_str = not in_str
            buf.append(ch)
        elif ch == "," and not in_str:
            out.append("".join(buf).strip()); buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf).strip())
    return out


def _unescape_e_string(body: str) -> str:
    """Undo the escapes _sql_literal applies inside an E'...' literal."""
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            mapped = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}.get(nxt)
            if mapped is not None:
                out.append(mapped)
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_sql_value(tok: str) -> Any:
    t = tok.strip()
    if t == "NULL":
        return None
    if t in ("true", "false"):
        return t == "true"
    is_e = t[:1] in ("E", "e") and t[1:2] == "'"
    if is_e or t.startswith("'"):
        body = t[1:] if is_e else t
        if "::" in body:
            body = body[: body.rindex("::")]
        inner = body[1:-1].replace("''", "'")
        if is_e:
            inner = _unescape_e_string(inner)
        if t.endswith("::jsonb"):
            try:
                return json.loads(inner)
            except Exception:
                return inner
        return inner
    # bare number
    try:
        return int(t)
    except ValueError:
        try:
            return float(t)
        except ValueError:
            return t


def sql_to_json(raw_sql: bytes) -> bytes:
    """Convert an Arc-generated SQL backup into the JSON backup format."""
    meta = parse_sql_meta(raw_sql)
    backup_tables: dict[str, list[str]] = meta.get("tables", {})
    text = raw_sql.decode("utf-8")

    tables: dict[str, TableData] = {}
    for name, cols in backup_tables.items():
        columns = [{"name": c, "pg": SYSTEM_PG.get(c, "text")} for c in cols]
        tables[name] = TableData(name, "", columns, [])

    for m in _INSERT_RE.finditer(text):
        table = m.group("table")
        col_names = [c.strip().strip('"') for c in m.group("cols").split(",")]
        values = [_parse_sql_value(v) for v in _split_sql_tuple(m.group("vals"))]
        if table in tables and len(col_names) == len(values):
            tables[table].rows.append(dict(zip(col_names, values)))

    backup = Backup(
        meta={"arc_backup_version": BACKUP_VERSION,
              "created_at": meta.get("created_at"),
              "converted_from": "sql"},
        tables=list(tables.values()),
    )
    return serialize_json(backup)


# ── Streaming backup (constant-memory, used by `arc db backup`) ──────────────
class _JsonStreamWriter:
    """Incrementally writes the exact document shape parse_json() reads."""

    def __init__(self, fh, meta: dict[str, Any]) -> None:
        self._fh = fh
        self._first_table = True
        self._first_row = True
        head = {**meta, "format": "json"}
        body = json.dumps(head, ensure_ascii=False, indent=2)
        # Drop the closing brace; the tables array and brace come later.
        self._fh.write(body[: body.rfind("}")].rstrip().rstrip(",") + ',\n  "tables": [')

    def begin_table(self, table: str, plugin: str, columns: list[dict[str, str]]) -> None:
        sep = "" if self._first_table else ","
        self._first_table = False
        self._first_row = True
        self._fh.write(
            f'{sep}\n    {{"table": {json.dumps(table)}, '
            f'"plugin": {json.dumps(plugin)}, '
            f'"columns": {json.dumps(columns, ensure_ascii=False)}, '
            f'"rows": ['
        )

    def write_row(self, row: dict[str, Any]) -> None:
        sep = "" if self._first_row else ","
        self._first_row = False
        self._fh.write(f"{sep}\n      {json.dumps(row, ensure_ascii=False)}")

    def end_table(self) -> None:
        self._fh.write("\n    ]}" if not self._first_row else "]}")

    def end(self) -> None:
        self._fh.write("\n  ]\n}\n")


class _SqlStreamWriter:
    """Incrementally writes the same statements serialize_sql() produces."""

    def __init__(self, fh, meta: dict[str, Any]) -> None:
        self._fh = fh
        self._fh.write(f"-- ARC-BACKUP-META: {json.dumps(meta)}\n")
        self._table = ""
        self._col_list = ""
        self._col_pg: dict[str, str] = {}
        self._columns: list[dict[str, str]] = []

    def begin_table(self, table: str, plugin: str, columns: list[dict[str, str]]) -> None:
        self._table = table
        self._columns = columns
        self._col_pg = {c["name"]: c["pg"] for c in columns}
        self._col_list = ", ".join(f'"{c["name"]}"' for c in columns)
        self._fh.write(f"\n-- table: {table}\n")

    def write_row(self, row: dict[str, Any]) -> None:
        vals = ", ".join(
            _sql_literal(row.get(c["name"]), self._col_pg[c["name"]])
            for c in self._columns
        )
        self._fh.write(
            f'INSERT INTO "{self._table}" ({self._col_list}) VALUES ({vals}) '
            f'ON CONFLICT ("id") DO NOTHING;\n'
        )

    def end_table(self) -> None:
        pass

    def end(self) -> None:
        pass


async def _registry_targets(conn, plugins: list[str] | None):
    """(targets, table->plugin, table->{field: pg}) from _field_registry."""
    from sqlalchemy import text

    rows = await conn.execute(text(
        "SELECT table_name, plugin, fld_id, field_name, type FROM _field_registry"
    ))
    reg: dict[str, dict[str, str]] = {}
    table_plugin: dict[str, str] = {}
    for table_name, plugin, _fid, field_name, type_ in rows:
        reg.setdefault(table_name, {})[field_name] = ARC_PG.get(type_, "text")
        table_plugin[table_name] = plugin
    wanted = set(plugins) if plugins else None
    targets = sorted(t for t, pl in table_plugin.items() if wanted is None or pl in wanted)
    return targets, table_plugin, reg, wanted


async def _ordered_columns(conn, tables: list[str]) -> dict[str, list[str]]:
    """Physical column order per table (matches SELECT of an explicit list)."""
    from sqlalchemy import text

    if not tables:
        return {}
    rows = await conn.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name = ANY(:t) "
        "ORDER BY table_name, ordinal_position"
    ), {"t": tables})
    out: dict[str, list[str]] = {}
    for table_name, column_name in rows:
        out.setdefault(table_name, []).append(column_name)
    return out


async def backup_to_file(
    conn, plugins: list[str] | None, dest: Path, fmt: str = "json"
) -> dict[str, Any]:
    """Stream a backup straight to *dest* — peak memory is one row batch.

    Produces byte-for-semantics the same formats as serialize_json /
    serialize_sql, so parse_json / restore_sql / sql_to_json all work on the
    output unchanged. Returns ``{"tables": n, "rows": n}``.
    """
    from sqlalchemy import text

    if fmt not in ("json", "sql"):
        raise ArcError(f"Unknown backup format '{fmt}'.", code="arc.db.backup.bad_format")

    targets, table_plugin, reg, wanted = await _registry_targets(conn, plugins)
    colmap = await _ordered_columns(conn, targets)

    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    table_count = 0
    row_count = 0

    with open(dest, "w", encoding="utf-8", newline="\n") as fh:
        if fmt == "json":
            writer: Any = _JsonStreamWriter(fh, {
                "arc_backup_version": BACKUP_VERSION,
                "created_at": created_at,
                "plugins": sorted(wanted) if wanted else "all",
            })
        else:
            writer = _SqlStreamWriter(fh, {
                "format": "sql",
                "arc_backup_version": BACKUP_VERSION,
                "created_at": created_at,
                "tables": {t: colmap.get(t, []) for t in targets},
            })

        for table in targets:
            col_names = colmap.get(table, [])
            if not col_names:
                continue
            columns = [
                {"name": n, "pg": SYSTEM_PG.get(n) or reg.get(table, {}).get(n, "text")}
                for n in col_names
            ]
            writer.begin_table(table, table_plugin[table], columns)
            col_list = ", ".join(f'"{n}"' for n in col_names)
            result = await conn.stream(
                text(f'SELECT {col_list} FROM "{table}"'),
                execution_options={"stream_results": True, "yield_per": 1000},
            )
            n_rows = 0
            async for row in result:
                writer.write_row(
                    {name: _to_jsonable(val) for name, val in zip(col_names, row)}
                )
                n_rows += 1
            writer.end_table()
            table_count += 1
            row_count += n_rows
            log.info("arc.db.backup_table", table=table, rows=n_rows)
        writer.end()

    return {"tables": table_count, "rows": row_count}


def encrypt_file(path: Path, passphrase: str) -> None:
    """Encrypt *path* in place (atomically via a sibling temp file).

    Fernet authenticates the whole message, so the serialized bytes are held
    in memory once during encryption — far below the old ceiling of the full
    row-object graph, but not zero. For very large encrypted backups prefer
    OS-level encryption (e.g. piping through age/gpg) of the plaintext file.
    """
    data = path.read_bytes()
    blob = encrypt_bytes(data, passphrase)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)


# ── Streaming backup (constant-memory path) ──────────────────────────────────
async def stream_backup_to_file(
    conn,
    plugins: list[str] | None,
    out_path: Path,
    fmt: str = "json",
    passphrase: str | None = None,
) -> dict[str, int]:
    """Back up directly to *out_path*, streaming table-by-table.

    Unlike ``read_backup`` + ``serialize_*`` (which materialise every row of
    every table in memory before a single byte is written), this path holds at
    most one server-side-cursor batch (1000 rows) at a time, so backup memory
    no longer scales with database size.

    The emitted bytes are IDENTICAL in format to the buffered path: JSON
    output parses with ``parse_json``; SQL output carries the same
    ``-- ARC-BACKUP-META:`` header and single-line INSERTs, so ``restore_sql``
    and ``sql_to_json`` work unchanged.

    Encryption caveat: Fernet authenticates the whole payload and cannot be
    streamed. With a passphrase the plaintext is streamed to a temporary file
    first, then encrypted in one pass — peak memory is the serialized BYTES
    (one buffer), still far below the Python object graph of the buffered
    path, but not constant. Unencrypted backups are fully streamed.

    Returns ``{"tables": n, "rows": n}``.
    """
    from sqlalchemy import text

    if fmt not in ("json", "sql"):
        raise ArcError(f"Unknown backup format '{fmt}'.", code="arc.db.backup.bad_format")

    # Registry pass — identical to read_backup.
    rows = await conn.execute(text(
        "SELECT table_name, plugin, fld_id, field_name, type FROM _field_registry"
    ))
    reg: dict[str, dict[str, str]] = {}
    table_plugin: dict[str, str] = {}
    for table_name, plugin, _fid, field_name, type_ in rows:
        reg.setdefault(table_name, {})[field_name] = ARC_PG.get(type_, "text")
        table_plugin[table_name] = plugin

    wanted = set(plugins) if plugins else None
    targets = sorted(t for t, pl in table_plugin.items() if wanted is None or pl in wanted)

    created_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Stream into a sibling temp file; rename on success so a crashed backup
    # never leaves a truncated file masquerading as a good one.
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")

    total_rows = 0

    async def _table_columns(result_keys: list[str], table: str) -> list[dict[str, str]]:
        return [
            {"name": n, "pg": SYSTEM_PG.get(n) or reg.get(table, {}).get(n, "text")}
            for n in result_keys
        ]

    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
            if fmt == "json":
                head = {
                    "arc_backup_version": BACKUP_VERSION,
                    "created_at": created_at,
                    "plugins": sorted(wanted) if wanted else "all",
                    "format": "json",
                }
                # Hand-rolled envelope so rows stream; payload stays valid
                # JSON and round-trips through parse_json unchanged.
                fh.write("{\n")
                for k, v in head.items():
                    fh.write(f"  {json.dumps(k)}: {json.dumps(v)},\n")
                fh.write('  "tables": [\n')
                for ti, table in enumerate(targets):
                    result = await conn.stream(
                        text(f'SELECT * FROM "{table}"'),
                        execution_options={"stream_results": True, "yield_per": 1000},
                    )
                    col_names = list(result.keys())
                    columns = await _table_columns(col_names, table)
                    fh.write("    {\n")
                    fh.write(f'      "table": {json.dumps(table)},\n')
                    fh.write(f'      "plugin": {json.dumps(table_plugin[table])},\n')
                    fh.write(f'      "columns": {json.dumps(columns)},\n')
                    fh.write('      "rows": [\n')
                    n = 0
                    async for row in result:
                        doc = {name: _to_jsonable(val) for name, val in zip(col_names, row)}
                        if n:
                            fh.write(",\n")
                        fh.write("        " + json.dumps(doc, ensure_ascii=False))
                        n += 1
                    total_rows += n
                    fh.write("\n      ]\n    }" if n else "      ]\n    }")
                    fh.write(",\n" if ti < len(targets) - 1 else "\n")
                    log.info("arc.db.backup_table", table=table, rows=n)
                fh.write("  ]\n}\n")
            else:  # sql
                meta = {
                    "format": "sql",
                    "arc_backup_version": BACKUP_VERSION,
                    "created_at": created_at,
                    "tables": {},
                }
                # SQL needs column names in the header, which we only know per
                # table; collect while streaming and patch the header last by
                # writing it to a separate first line placeholder is fragile —
                # instead do a cheap zero-row metadata pass first.
                header_tables: dict[str, list[str]] = {}
                for table in targets:
                    cols_res = await conn.execute(text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t "
                        "ORDER BY ordinal_position"
                    ), {"t": table})
                    header_tables[table] = [r[0] for r in cols_res]
                meta["tables"] = header_tables
                fh.write(f"-- ARC-BACKUP-META: {json.dumps(meta)}\n")
                for table in targets:
                    col_names = header_tables[table]
                    columns = await _table_columns(col_names, table)
                    col_pg = {c["name"]: c["pg"] for c in columns}
                    col_list = ", ".join(f'"{n}"' for n in col_names)
                    select_cols = ", ".join(f'"{n}"' for n in col_names)
                    result = await conn.stream(
                        text(f'SELECT {select_cols} FROM "{table}"'),
                        execution_options={"stream_results": True, "yield_per": 1000},
                    )
                    fh.write(f"\n-- table: {table}\n")
                    n = 0
                    async for row in result:
                        vals = ", ".join(
                            _sql_literal(_to_jsonable(v), col_pg[c]) for c, v in zip(col_names, row)
                        )
                        fh.write(
                            f'INSERT INTO "{table}" ({col_list}) VALUES ({vals}) '
                            f'ON CONFLICT ("id") DO NOTHING;\n'
                        )
                        n += 1
                    total_rows += n
                    log.info("arc.db.backup_table", table=table, rows=n)

        if passphrase:
            # Fernet is not streamable — one read of the serialized bytes.
            data = tmp_path.read_bytes()
            out_path.write_bytes(encrypt_bytes(data, passphrase))
            tmp_path.unlink(missing_ok=True)
        else:
            tmp_path.replace(out_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return {"tables": len(targets), "rows": total_rows}