"""
arc.plugins.db.migrations.system
==============================
System objects Arc installs before any user table. Each entry is ONE SQL
statement — asyncpg rejects multi-statement strings in a prepared statement.

Installed on every `arc db migrate` (all idempotent):

    uuid_generate_v7()   time-ordered UUIDs (RFC 9562 v7) for the id column.
                         PostgreSQL 16 has no native uuidv7(); this plpgsql
                         function provides it. Better index locality than v4
                         and an embedded timestamp for cursor pagination.

    _field_registry      every column Arc has ever created: fld_id, table,
                         field_name, type, reqd, max_length, plugin. This is
                         the source of truth the patch compiler diffs against.
                         Rows are inserted on ADD, updated on RENAME/ALTER,
                         and DELETED on DROP.

    _patch_history       applied patch_ids — a patch is never run twice.

    _trash               destination for deletions. drop_type distinguishes a
                         deleted row ('row') from a dropped column ('column').
"""

from __future__ import annotations

# ── UUID v7 generator (canonical plpgsql implementation, RFC 9562) ──────────
UUID_V7_FUNCTION = """
CREATE OR REPLACE FUNCTION uuid_generate_v7()
RETURNS uuid
AS $$
BEGIN
    -- Start from a random v4 UUID, overlay the current unix-millis timestamp
    -- onto the first 6 bytes, then set the version nibble to 7.
    RETURN encode(
        set_bit(
            set_bit(
                overlay(
                    uuid_send(gen_random_uuid())
                    PLACING substring(
                        int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint)
                        FROM 3
                    )
                    FROM 1 FOR 6
                ),
                52, 1
            ),
            53, 1
        ),
        'hex'
    )::uuid;
END;
$$ LANGUAGE plpgsql VOLATILE;
""".strip()


# ── System tables ───────────────────────────────────────────────────────────
FIELD_REGISTRY = """
CREATE TABLE IF NOT EXISTS _field_registry (
    id          BIGSERIAL    PRIMARY KEY,
    fld_id      VARCHAR(4)   NOT NULL,
    table_name  VARCHAR(255) NOT NULL,
    field_name  VARCHAR(255) NOT NULL,
    type        VARCHAR(32)  NOT NULL,
    reqd        BOOLEAN      NOT NULL DEFAULT false,
    max_length  INTEGER      NULL,
    plugin      VARCHAR(128) NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    UNIQUE (fld_id, table_name)
);
""".strip()

PATCH_HISTORY = """
CREATE TABLE IF NOT EXISTS _patch_history (
    patch_id    VARCHAR(128) PRIMARY KEY,
    plugin      VARCHAR(128) NOT NULL,
    table_name  VARCHAR(255) NOT NULL,
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    description TEXT         NULL
);
""".strip()

# record_id is NULL for column drops; data holds the row JSON (row drop) or the
# {table, column, fld_id, type, values:[{id,value}...]} payload (column drop).
TRASH = """
CREATE TABLE IF NOT EXISTS _trash (
    id          BIGSERIAL    PRIMARY KEY,
    table_name  VARCHAR(255) NOT NULL,
    record_id   UUID         NULL,
    drop_type   VARCHAR(16)  NOT NULL DEFAULT 'row',
    data        JSONB        NOT NULL,
    deleted_by  VARCHAR(255) NULL,
    deleted_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    restored_at TIMESTAMPTZ  NULL
);
""".strip()


# Order matters: the UUID function before any table that defaults to it,
# and the system tables before user tables.
SYSTEM_DDL: list[str] = [
    UUID_V7_FUNCTION,
    FIELD_REGISTRY,
    PATCH_HISTORY,
    TRASH,
    # Self-healing: add columns that older _field_registry installs are missing.
    # ADD COLUMN IF NOT EXISTS is idempotent — safe to run every migration.
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS type VARCHAR(32) NOT NULL DEFAULT \'Data\'',
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS reqd BOOLEAN NOT NULL DEFAULT false',
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS max_length INTEGER NULL',
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()',
]

DROP_TYPE_ROW = "row"
DROP_TYPE_COLUMN = "column"