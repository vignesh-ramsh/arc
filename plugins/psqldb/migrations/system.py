"""
arc.plugins.psqldb.migrations.system
==============================
System objects Arc installs before any user table. Each entry is ONE SQL
statement — asyncpg rejects multi-statement strings in a prepared statement.

Installed on every `arc db migrate` (all idempotent):

    uuid_generate_v7()    time-ordered UUIDs (RFC 9562 v7) for the id column.
    arc_set_updated_at()  shared BEFORE UPDATE trigger function.
    _field_registry       every Arc-managed column + virtual (Table) entries.
                          Source of truth for the patch compiler AND for relay's
                          delete-time referential checks (Link / Table types).
    _patch_history        applied patch_ids — a patch is never run twice.
    _trash                destination for soft-deleted rows and dropped columns.
"""

from __future__ import annotations

# ── UUID v7 generator (canonical plpgsql implementation, RFC 9562) ──────────
UUID_V7_FUNCTION = """
CREATE OR REPLACE FUNCTION uuid_generate_v7()
RETURNS uuid
AS $$
BEGIN
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

# ── updated_at maintenance ───────────────────────────────────────────────────
UPDATED_AT_FUNCTION = """
CREATE OR REPLACE FUNCTION arc_set_updated_at()
RETURNS trigger
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""".strip()


# ── System tables ───────────────────────────────────────────────────────────
# link_table and is_virtual are part of the canonical schema as of this revision:
#   * link_table: FK target for Link, child table name for Table (virtual)
#   * is_virtual: true for Table-type rows (metadata only, no physical column)
# Existing installs are healed via the ADD COLUMN IF NOT EXISTS block below.
FIELD_REGISTRY = """
CREATE TABLE IF NOT EXISTS _field_registry (
    id          BIGSERIAL    PRIMARY KEY,
    fld_id      VARCHAR(32)  NOT NULL,
    table_name  VARCHAR(255) NOT NULL,
    field_name  VARCHAR(255) NOT NULL,
    type        VARCHAR(32)  NOT NULL,
    reqd        BOOLEAN      NOT NULL DEFAULT false,
    max_length  INTEGER      NULL,
    link_table  VARCHAR(255) NULL,
    is_virtual  BOOLEAN      NOT NULL DEFAULT false,
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
    UPDATED_AT_FUNCTION,
    FIELD_REGISTRY,
    PATCH_HISTORY,
    TRASH,
    # Self-healing: add columns that older _field_registry installs are missing.
    # ADD COLUMN IF NOT EXISTS is idempotent — safe to run every migration.
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS type VARCHAR(32) NOT NULL DEFAULT \'Data\'',
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS reqd BOOLEAN NOT NULL DEFAULT false',
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS max_length INTEGER NULL',
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()',
    # New columns (this revision):
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS link_table VARCHAR(255) NULL',
    'ALTER TABLE _field_registry ADD COLUMN IF NOT EXISTS is_virtual BOOLEAN NOT NULL DEFAULT false',
    # fld_id was VARCHAR(4); virtual fields use longer internal keys like
    # "_v_<field_name>". Widen if narrower than 32.
    'ALTER TABLE _field_registry ALTER COLUMN fld_id TYPE VARCHAR(32)',
    # Index lookups by (link_table, type) — relay's delete check uses this.
    'CREATE INDEX IF NOT EXISTS ix__field_registry_link ON _field_registry (link_table, type) WHERE link_table IS NOT NULL',
    # Trash cleanup scans by deleted_at; keep it indexed.
    'CREATE INDEX IF NOT EXISTS ix__trash_deleted_at ON _trash (deleted_at)',
]

DROP_TYPE_ROW = "row"
DROP_TYPE_COLUMN = "column"


# ── Per-table infrastructure (idempotent — ensured on every migrate) ─────────
def table_infra_statements(table: str, link_columns: list[str] | None = None) -> list[str]:
    """Return idempotent statements ensuring a table's system infrastructure.

    * updated_at trigger (CREATE OR REPLACE TRIGGER — requires PG14+).
    * (updated_at DESC, id) index backing the API's default list ordering.
    * one index per Link column so UUID FK lookups/joins don't seq-scan.
    """
    stmts = [
        (
            f'CREATE OR REPLACE TRIGGER "trg_{table}_updated_at" '
            f'BEFORE UPDATE ON "{table}" '
            f"FOR EACH ROW EXECUTE FUNCTION arc_set_updated_at();"
        ),
        (
            f'CREATE INDEX IF NOT EXISTS "ix_{table}_updated_at" '
            f'ON "{table}" (updated_at DESC, id);'
        ),
    ]
    for col in link_columns or []:
        stmts.append(
            f'CREATE INDEX IF NOT EXISTS "ix_{table}_{col}" ON "{table}" ("{col}");'
        )
    return stmts