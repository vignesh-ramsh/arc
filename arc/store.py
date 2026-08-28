"""
arc.store
------------------
SQLite-backed settings + secrets store for one ARC project — replaces the
old two-file split (`.arc/arc.toml`'s `[settings]`/`[secrets].declared` +
`.arc/arc.secrets`, one Fernet-encrypted JSON blob for every secret) with
one `.arc/arc.store.db` holding three tables:

  * `setting`            one row per key, plain or secret. A secret row's
                          `value` column is always NULL — the real value
                          lives in `secret` below; this table only ever
                          carries the bookkeeping (`is_secret`) needed to
                          know how to read a key at all.
  * `secret`              ciphertext for secret keys only, one row per key
                          (Fernet-encrypted independently — writing one
                          secret never re-encrypts another's ciphertext,
                          unlike the old whole-store-blob format).
  * `secret_access_log`   one row per REVEALED read only (`get(key,
                          reveal=True)`). A masked/redacted read never
                          writes here — logging every masked read would
                          spam this table every time a superuser's
                          Settings page renders the key list without
                          revealing anything.

`.arc/arc.mkey` stays a plain file outside this database on purpose — it's
the key that unlocks the `secret` table, so it cannot live inside the
store it unlocks.

No hand-rolled cache/invalidation layer here (unlike the old file-based
store's (mtime_ns, size) stat trick) — WAL mode gives every connection,
including another process's, a correct, current view of the last
committed write for free.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import InvalidToken

from .secrets import SecretsError, _fernet_from_mkey

_SCHEMA = """
CREATE TABLE IF NOT EXISTS setting (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    is_secret   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS secret (
    key         TEXT PRIMARY KEY REFERENCES setting(key),
    ciphertext  BLOB NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
CREATE TABLE IF NOT EXISTS secret_access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    accessed_by TEXT
);
"""

# One cached connection per store path per process — mirrors how the old
# file-based stores were reopened per call, just with a real handle instead
# of stat()ing a path each time. Never closed explicitly: it lives for the
# process's lifetime, same as any other kernel-level resource.
_connections: dict[Path, sqlite3.Connection] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = _connections.get(db_path)
    if conn is not None:
        return conn
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    _connections[db_path] = conn
    return conn


def get_setting(conn: sqlite3.Connection, key: str) -> tuple[str | None, bool]:
    """(value, is_secret). value is always None for a secret row — the real
    value only ever comes back through reveal_secret()."""
    row = conn.execute("SELECT value, is_secret FROM setting WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None, False
    value, is_secret = row
    return (None if is_secret else value), bool(is_secret)


def is_secret(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT is_secret FROM setting WHERE key = ?", (key,)).fetchone()
    return bool(row and row[0])


def secret_has_value(conn: sqlite3.Connection, key: str) -> bool:
    """Cheap existence check for a masked (reveal=False) read — whether a
    secret has a real value at all, without decrypting it or writing a
    secret_access_log row. Only reveal_secret() below ever decrypts/logs."""
    row = conn.execute("SELECT 1 FROM secret WHERE key = ?", (key,)).fetchone()
    return row is not None


def set_plain(conn: sqlite3.Connection, key: str, value: str, updated_by: str | None) -> None:
    conn.execute(
        "INSERT INTO setting(key, value, is_secret, updated_at, updated_by) "
        "VALUES (?, ?, 0, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, is_secret=0, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, value, _now(), updated_by),
    )
    conn.commit()


def declare_secret_key(conn: sqlite3.Connection, key: str) -> None:
    """Marks `key` as secret with no value yet — the persistence half of
    declare(secret=True) before any set() has happened, so a get() in the
    meantime already knows to redact rather than treating it as a plain
    unset key. A no-op if the row already exists (declare() is idempotent)."""
    conn.execute(
        "INSERT OR IGNORE INTO setting(key, value, is_secret, updated_at, updated_by) "
        "VALUES (?, NULL, 1, ?, 'declare')",
        (key, _now()),
    )
    conn.commit()


def set_secret(
    conn: sqlite3.Connection, mkey_path: Path, key: str, value: str, updated_by: str | None
) -> None:
    fernet = _fernet_from_mkey(mkey_path)
    ciphertext = fernet.encrypt(value.encode("utf-8"))
    now = _now()
    conn.execute(
        "INSERT INTO setting(key, value, is_secret, updated_at, updated_by) "
        "VALUES (?, NULL, 1, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=NULL, is_secret=1, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, now, updated_by),
    )
    conn.execute(
        "INSERT INTO secret(key, ciphertext, updated_at, updated_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET ciphertext=excluded.ciphertext, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, ciphertext, now, updated_by),
    )
    conn.commit()


def reveal_secret(
    conn: sqlite3.Connection, mkey_path: Path, key: str, accessed_by: str | None
) -> str | None:
    """The real value, decrypted — and the ONLY function in this module
    that writes a secret_access_log row. Returns None for a declared-but-
    never-set secret (no row in `secret` yet) without logging anything —
    there's no real value that was actually revealed."""
    row = conn.execute("SELECT ciphertext FROM secret WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    fernet = _fernet_from_mkey(mkey_path)
    try:
        plaintext = fernet.decrypt(row[0])
    except InvalidToken as exc:
        raise SecretsError(
            f"Could not decrypt secret '{key}' — wrong master key, or the store is corrupt."
        ) from exc
    conn.execute(
        "INSERT INTO secret_access_log(key, accessed_at, accessed_by) VALUES (?, ?, ?)",
        (key, _now(), accessed_by),
    )
    conn.commit()
    return plaintext.decode("utf-8")


def delete_key(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT key FROM setting WHERE key = ?", (key,)).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM secret WHERE key = ?", (key,))
    conn.execute("DELETE FROM setting WHERE key = ?", (key,))
    conn.commit()
    return True


def list_all(conn: sqlite3.Connection) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key, value, is_secret_flag in conn.execute("SELECT key, value, is_secret FROM setting"):
        out[key] = {"value": value, "is_secret": bool(is_secret_flag)}
    return out


def access_log(conn: sqlite3.Connection, key: str | None = None, limit: int = 100) -> list[dict]:
    if key is not None:
        rows = conn.execute(
            "SELECT key, accessed_at, accessed_by FROM secret_access_log "
            "WHERE key = ? ORDER BY id DESC LIMIT ?",
            (key, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT key, accessed_at, accessed_by FROM secret_access_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"key": k, "accessed_at": at, "accessed_by": by} for k, at, by in rows]
