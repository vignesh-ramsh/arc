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
  * `secret_access_log`   an explicit audit trail, written ONLY by
                          `record_access()` — i.e. by a caller that is
                          showing a secret to a person (the Admin UI's
                          reveal action records who and when). Reading a
                          secret never writes here: `get(key, reveal=True)`
                          is also how the system itself reads its own
                          secrets on every request (e.g. authn's CSRF
                          key), and a synchronous INSERT+COMMIT on a
                          SQLite file shared by every worker process, per
                          request, serialised them all behind SQLite's
                          single writer lock.

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

from . import permissions
from .secrets import SecretsError, _fernet_from_mkey

_SCHEMA = """
CREATE TABLE IF NOT EXISTS setting (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    is_secret   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT,
    description         TEXT,
    description_edited  INTEGER NOT NULL DEFAULT 0,
    description_by      TEXT,
    description_at      TEXT,
    type                TEXT
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

#: The columns added to `setting` after the first release, and the SQL that adds each to a store made before it.
#: `description` is the text shown on the admin Settings page: the code's text copied in at boot, or what an
#: administrator wrote (`description_edited` = 1, by `description_by` at `description_at`). Kept next to the
#: value it describes, but independent of it: a value change never touches them, and clearing a value keeps them.
#: `type` is the type (`str`, `int`, `float`, `bool`) given to a key added by hand from the admin page, which
#: no code declares one for; a type the code declares always wins over it. It is set when the key is created and
#: never changed afterwards.
_ADDED_COLUMNS = (
    ("description", "TEXT"),
    ("description_edited", "INTEGER NOT NULL DEFAULT 0"),
    ("description_by", "TEXT"),
    ("description_at", "TEXT"),
    ("type", "TEXT"),
)

#: The types a key added by hand may be given: the same closed set `declare(type=...)` accepts.
STORED_TYPES = ("str", "int", "float", "bool")

#: The longest description an administrator may save, in characters.
MAX_DESCRIPTION_CHARS = 2000


def _ensure_added_columns(conn: sqlite3.Connection) -> None:
    """Add whichever of the later-added columns the `setting` table lacks. Safe to run any number of times and from
    several processes at once: a column another process added a moment ago is not an error."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(setting)")}
    for name, ddl in _ADDED_COLUMNS:
        if name in have:
            continue
        try:
            conn.execute(f"ALTER TABLE setting ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.commit()


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
    # A new store is private to its owner from the start: created 0600 BEFORE SQLite opens it, so
    # that its -wal and -shm files (which take the mode of the main file) are too. A store that
    # already exists is never touched here - `arc doctor --fix` repairs an old one explicitly.
    permissions.create_private_dir(db_path.parent)
    permissions.create_private_file(db_path)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    _ensure_added_columns(conn)
    _connections[db_path] = conn
    return conn


def get_setting_typed(conn: sqlite3.Connection, key: str) -> tuple[str | None, bool, str | None]:
    """(value, is_secret, type) in ONE query, which is what get() needs on every read. value is always None for
    a secret row — the real value only ever comes back through reveal_secret(). type is the one a key added by
    hand was given, or None."""
    row = conn.execute("SELECT value, is_secret, type FROM setting WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None, False, None
    value, is_secret, type_ = row
    return (None if is_secret else value), bool(is_secret), type_


def get_setting(conn: sqlite3.Connection, key: str) -> tuple[str | None, bool]:
    """(value, is_secret). value is always None for a secret row — the real
    value only ever comes back through reveal_secret()."""
    value, is_secret, _ = get_setting_typed(conn, key)
    return value, is_secret


def key_exists(conn: sqlite3.Connection, key: str) -> bool:
    """Whether the store has a row for `key`, even one with no value (a key whose description was copied in)."""
    return conn.execute("SELECT 1 FROM setting WHERE key = ?", (key,)).fetchone() is not None


def is_secret(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT is_secret FROM setting WHERE key = ?", (key,)).fetchone()
    return bool(row and row[0])


def secret_has_value(conn: sqlite3.Connection, key: str) -> bool:
    """Cheap existence check for a masked (reveal=False) read — whether a
    secret has a real value at all, without decrypting it. Decrypting is
    reveal_secret()'s job below."""
    row = conn.execute("SELECT 1 FROM secret WHERE key = ?", (key,)).fetchone()
    return row is not None


#: Bumped by every function here that changes a setting's VALUE. Read by
#: SettingsManager to invalidate its own read cache.
#:
#: Needed alongside `PRAGMA data_version` rather than instead of it,
#: because the two catch different writers and neither catches both.
#: data_version only moves for a write committed by a DIFFERENT
#: connection — but open_db() hands every SettingsManager in a process
#: the SAME cached connection, so one manager's write is invisible to
#: another manager's data_version check even though they're separate
#: objects with separate caches. (Exactly the shape
#: test_gateway_error_pages.py exercises: a second SettingsManager
#: standing in for an `arc set-maintenance` CLI process, toggling the
#: setting out from under a running gateway.) This counter covers every
#: in-process writer; data_version covers genuinely other processes.
_write_epoch = 0


def write_epoch() -> int:
    """Current value of the process-wide settings-write counter — see
    _write_epoch's own comment for what it's for and why data_version
    alone isn't enough."""
    return _write_epoch


def _bump_write_epoch() -> None:
    global _write_epoch
    _write_epoch += 1


def _first_write(type_: str | None, description: str | None, by: str | None, now: str) -> tuple:
    """The `type` and description columns for a row being created. They are applied only when the key is new:
    a key that already has a row keeps its type and its description, whatever a later write says."""
    return (type_, description, 1 if description else 0, by if description else None, now if description else None)


def set_plain(
    conn: sqlite3.Connection,
    key: str,
    value: str,
    updated_by: str | None,
    type_: str | None = None,
    description: str | None = None,
) -> None:
    """Store a plain value. `type_` and `description` are the new key's own, written once, with the row; an
    existing key keeps what it has (see _first_write)."""
    _bump_write_epoch()
    now = _now()
    conn.execute(
        "INSERT INTO setting(key, value, is_secret, updated_at, updated_by, "
        "type, description, description_edited, description_by, description_at) "
        "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, is_secret=0, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, value, now, updated_by, *_first_write(type_, description, updated_by, now)),
    )
    conn.commit()


def declare_secret_key(conn: sqlite3.Connection, key: str) -> None:
    """Marks `key` as secret with no value yet — the persistence half of
    declare(secret=True) before any set() has happened, so a get() in the
    meantime already knows to redact rather than treating it as a plain
    unset key. A no-op if the key is already a secret (declare() is idempotent).

    A key that already has a row with no value (its description was copied in while an earlier release declared
    it plain) is turned into a secret: leaving it plain would stop it being redacted. A row that holds a plain
    VALUE is left alone; declare() refuses that case before it gets here."""
    _bump_write_epoch()
    conn.execute(
        "INSERT INTO setting(key, value, is_secret, updated_at, updated_by) "
        "VALUES (?, NULL, 1, ?, 'declare') "
        "ON CONFLICT(key) DO UPDATE SET is_secret=1 WHERE is_secret=0 AND value IS NULL",
        (key, _now()),
    )
    conn.commit()


def set_secret(
    conn: sqlite3.Connection,
    mkey_path: Path,
    key: str,
    value: str,
    updated_by: str | None,
    type_: str | None = None,
    description: str | None = None,
) -> None:
    """Store a secret. `type_` and `description` are the new key's own, as in set_plain."""
    _bump_write_epoch()
    fernet = _fernet_from_mkey(mkey_path)
    ciphertext = fernet.encrypt(value.encode("utf-8"))
    now = _now()
    conn.execute(
        "INSERT INTO setting(key, value, is_secret, updated_at, updated_by, "
        "type, description, description_edited, description_by, description_at) "
        "VALUES (?, NULL, 1, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=NULL, is_secret=1, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, now, updated_by, *_first_write(type_, description, updated_by, now)),
    )
    conn.execute(
        "INSERT INTO secret(key, ciphertext, updated_at, updated_by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET ciphertext=excluded.ciphertext, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (key, ciphertext, now, updated_by),
    )
    conn.commit()


def all_secret_values(conn: sqlite3.Connection, mkey_path: Path) -> list[str]:
    """Every stored secret's decrypted value, in ONE query.

    Exists for exactly one job: scrubbing real secret values out of text
    that is about to be shown to someone (gateway's own traceback
    redaction). That is the opposite of a reveal — nobody sees these
    values, they are being blacklisted so they CAN'T be seen.

    NEVER use this to DISPLAY a secret to a caller: a caller that shows a
    secret to a person must record that with record_access(). A corrupt/undecryptable row is skipped rather than
    raising: redaction is best-effort by nature, and one bad row must not
    turn an ordinary 500 into a second, different failure."""
    values: list[str] = []
    fernet = _fernet_from_mkey(mkey_path)
    for (ciphertext,) in conn.execute("SELECT ciphertext FROM secret"):
        try:
            values.append(fernet.decrypt(ciphertext).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError):
            continue
    return values


def reveal_secret(conn: sqlite3.Connection, mkey_path: Path, key: str) -> str | None:
    """The real value, decrypted. A pure read: it writes nothing, so it is
    safe on a hot path (the system reads its own secrets with it on every
    request). Returns None for a declared-but-never-set secret (no row in
    `secret` yet).

    It does NOT write an audit row. A caller that shows the value to a
    person records that explicitly with record_access()."""
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
    return plaintext.decode("utf-8")


def record_access(conn: sqlite3.Connection, key: str, accessed_by: str | None) -> None:
    """Append one audit row: `accessed_by` was shown the value of secret
    `key`, now. The only writer of secret_access_log. Call it from the one
    place that surfaces a secret to a person (the Admin UI's reveal
    action) — never from code that merely uses a secret."""
    conn.execute(
        "INSERT INTO secret_access_log(key, accessed_at, accessed_by) VALUES (?, ?, ?)",
        (key, _now(), accessed_by),
    )
    conn.commit()


def delete_key(conn: sqlite3.Connection, key: str) -> bool:
    """Clear a key's value, the way back to its default (`arc settings delete`).

    A key whose row carries a description keeps the row (and so the description and, for a secret, the fact that
    it is one): only the value and any ciphertext go, and the answer is whether there was a value to clear. A key
    without a description disappears entirely, as it always did, and the answer is whether it existed."""
    row = conn.execute("SELECT value, description FROM setting WHERE key = ?", (key,)).fetchone()
    if row is None:
        return False
    value, description = row
    if description is None:
        _bump_write_epoch()
        conn.execute("DELETE FROM secret WHERE key = ?", (key,))
        conn.execute("DELETE FROM setting WHERE key = ?", (key,))
        conn.commit()
        return True
    had_value = value is not None or secret_has_value(conn, key)
    if not had_value:
        return False
    _bump_write_epoch()
    conn.execute("DELETE FROM secret WHERE key = ?", (key,))
    conn.execute("UPDATE setting SET value = NULL, updated_at = ?, updated_by = 'delete' WHERE key = ?", (_now(), key))
    conn.commit()
    return True


def list_all(conn: sqlite3.Connection) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key, value, is_secret_flag, description, edited, by, at, type_ in conn.execute(
        "SELECT key, value, is_secret, description, description_edited, description_by, description_at, type "
        "FROM setting"
    ):
        out[key] = {
            "value": value,
            "is_secret": bool(is_secret_flag),
            "type": type_,
            "description": description,
            "description_edited": bool(edited),
            "description_by": by,
            "description_at": at,
        }
    return out


def _ensure_row(conn: sqlite3.Connection, key: str) -> None:
    """A row for a key that is known (declared) but has never been set: no value, not secret. A key that already
    has a row is left alone, whatever it holds."""
    conn.execute(
        "INSERT OR IGNORE INTO setting(key, value, is_secret, updated_at, updated_by) VALUES (?, NULL, 0, ?, 'declare')",
        (key, _now()),
    )


def sync_descriptions(conn: sqlite3.Connection, docs: dict[str, str]) -> int:
    """Copy the descriptions the code declares into the store: a row for a key that has none yet, and a changed
    text for a key nobody has edited. An administrator's edit is never overwritten. Returns how many rows it wrote;
    when nothing differs it executes no statement at all, because every ARC process runs this when it starts and
    they start together. It changes no value, so it does not touch the write counter that the read caches watch."""
    if not docs:
        return 0
    have = {k: (d, bool(e)) for k, d, e in conn.execute("SELECT key, description, description_edited FROM setting")}
    now = _now()
    new = [(key, now, doc) for key, doc in docs.items() if key not in have]
    changed = [(doc, key) for key, doc in docs.items() if key in have and not have[key][1] and have[key][0] != doc]
    if not new and not changed:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO setting(key, value, is_secret, updated_at, updated_by, description) "
        "VALUES (?, NULL, 0, ?, 'declare', ?)",
        new,
    )
    conn.executemany("UPDATE setting SET description = ? WHERE key = ? AND description_edited = 0", changed)
    conn.commit()
    return len(new) + len(changed)


def set_description(conn: sqlite3.Connection, key: str, text: str, by: str | None, *, create: bool = False) -> bool:
    """An administrator's description for `key`, kept with who and when. `create` adds the row first for a key that
    is declared but has never been copied in. False when there is no such key. Changes no value."""
    if create:
        _ensure_row(conn, key)
    cursor = conn.execute(
        "UPDATE setting SET description = ?, description_edited = 1, description_by = ?, description_at = ? "
        "WHERE key = ?",
        (text, by, _now(), key),
    )
    conn.commit()
    return cursor.rowcount == 1


def reset_description(conn: sqlite3.Connection, key: str, original: str | None, *, create: bool = False) -> bool:
    """Back to the code's text (`original`, or none when the code declares none). A plain key that then has neither
    a value nor a description has no reason to keep its row, and loses it; a secret keeps its row so that it stays
    a secret. False when there is no such key."""
    if create and original:
        _ensure_row(conn, key)
    cursor = conn.execute(
        "UPDATE setting SET description = ?, description_edited = 0, description_by = NULL, description_at = NULL "
        "WHERE key = ?",
        (original or None, key),
    )
    if cursor.rowcount == 1 and not original:
        conn.execute("DELETE FROM setting WHERE key = ? AND value IS NULL AND is_secret = 0 AND description IS NULL", (key,))
    conn.commit()
    return cursor.rowcount == 1


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
