"""
arc.plugins.db.backup
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
    "Data": "text", "Text": "text", "Int": "integer", "Float": "double precision",
    "Decimal": "numeric", "Bool": "boolean", "Date": "date", "Datetime": "timestamptz",
    "JSON": "jsonb", "Link": "uuid",
}


# ── Models ───────────────────────────────────────────────────────────────────
@dataclass
class TableData:
    table: str
    plugin: str
    columns: list[dict[str, str]]   # [{"name":..., "pg":...}]
    rows: list[dict[str, Any]]


@dataclass
class Backup:
    meta: dict[str, Any]
    tables: list[TableData] = field(default_factory=list)


# ── Value coercion ───────────────────────────────────────────────────────────
def _to_jsonable(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(bytes(v)).decode()
    if isinstance(v, (dict, list)):
        return v
    return str(v)


def _sql_literal(v: Any, pg: str) -> str:
    if v is None:
        return "NULL"
    if pg == "jsonb":
        payload = json.dumps(v) if isinstance(v, (dict, list)) else (v if isinstance(v, str) else json.dumps(v))
        return "'" + payload.replace("'", "''") + "'::jsonb"
    if pg in ("integer", "numeric", "double precision"):
        return str(v)
    if pg == "boolean":
        return "true" if v else "false"
    # uuid / text / date / timestamptz → quoted literal
    return "'" + _to_jsonable(v).replace("'", "''") + "'"


# ── Read DB → Backup ─────────────────────────────────────────────────────────
async def read_backup(conn, plugins: list[str] | None) -> Backup:
    from sqlalchemy import text

    # plugin -> tables, and table field types from the registry.
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
        result = await conn.execute(text(f'SELECT * FROM "{table}"'))
        col_names = list(result.keys())
        columns = []
        for name in col_names:
            pg = SYSTEM_PG.get(name) or reg.get(table, {}).get(name, "text")
            columns.append({"name": name, "pg": pg})
        data_rows = [
            {name: _to_jsonable(val) for name, val in zip(col_names, row)}
            for row in result
        ]
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


async def restore_json(conn, backup: Backup) -> dict[str, int]:
    """Partial restore: only columns that currently exist; skip missing tables."""
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
        for row in t.rows:
            params = {}
            for c in usable:
                v = row.get(c["name"])
                if c["pg"] == "jsonb" and v is not None and not isinstance(v, str):
                    v = json.dumps(v)
                params[c["name"]] = None if v is None else (v if isinstance(v, str) else str(v) if not isinstance(v, bool) else ("true" if v else "false"))
            await conn.execute(sql, params)
            inserted += 1
    return {"inserted": inserted, "skipped_tables": skipped_tables}


async def restore_sql(conn, raw_sql: bytes) -> dict[str, int]:
    """Strict restore: validate every table's columns match, then execute."""
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
    return table


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

    from arc.plugins.db.migrations.schema import render_column_type
    col_type = render_column_type(arc_type, max_length)

    # Re-add the column (nullable first; values backfilled next).
    await conn.execute(text(
        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {col_type} NULL'
    ))
    pg = ARC_PG.get(arc_type, "text")
    for item in data.get("values", []):
        rid = item.get("id")
        val = item.get("value")
        if rid is None:
            continue
        if pg == "jsonb" and val is not None and not isinstance(val, str):
            val = json.dumps(val)
        await conn.execute(
            text(f'UPDATE "{table}" SET "{column}" = CAST(:v AS {pg}) WHERE id = CAST(:rid AS uuid)'),
            {"v": None if val is None else (val if isinstance(val, str) else str(val)), "rid": str(rid)},
        )
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


def _parse_sql_value(tok: str) -> Any:
    t = tok.strip()
    if t == "NULL":
        return None
    if t in ("true", "false"):
        return t == "true"
    if t.startswith("'"):
        body = t
        if "::" in t:
            body = t[: t.rindex("::")]
        inner = body[1:-1].replace("''", "'")
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