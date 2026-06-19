"""
arc.plugins.psqldb.migrations.migrator
================================
The migration pipeline.

    1. read DB state         (existing tables/columns, _field_registry, patches)
    2. build a plan (pure)   (system DDL, CREATE TABLE, patch changes)
    3. lint                  (duplicate tables, destructive gate, ...)
    4. execute               (one statement per call; each op atomic)

Design rules:
  * schemas create NEW tables only; modifications go through patches.
  * plan() is pure — it never touches the database. Only execute() does.
  * read and DDL use SEPARATE connections.
  * one text() per statement — asyncpg rejects multi-statement strings.
  * idempotent — safe to run repeatedly.
  * PLAN IS ORDER-INDEPENDENT: all schemas across all sources are planned
    first, then all patches. A patch from plugin A onto a table that plugin B
    creates in the same run is applied correctly regardless of which order
    the sources were discovered in (previously it depended on arc.lock order).
  * EACH OP IS ATOMIC: PostgreSQL DDL is transactional, so every MigrationOp
    (its DDL + its registry sync) runs inside one transaction. A failure rolls
    the whole op back — the registry can never disagree with the live schema,
    which previously could escalate an interrupted safe RENAME into a
    destructive DROP+ADD on the next run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from arc.kernel.logger import get_logger
from plugins.psqldb.migrations import ddl_linter
from plugins.psqldb.migrations.ddl_linter import LintIssue
from plugins.psqldb.migrations.patch_compiler import (
    ColumnChange,
    PatchCompiler,
    PatchDef,
    RegistryEntry,
    _q,
    compute_changes,
    generate_sql,
    registry_upsert,
)
from plugins.psqldb.migrations.schema import SchemaCompiler, TableSchema, compile_create_table
from plugins.psqldb.migrations.system import SYSTEM_DDL, table_infra_statements

log = get_logger(__name__)


# ── What a plugin contributes to db.schema_sources ───────────────────────────
@dataclass
class SchemaSource:
    """A plugin's migration inputs. ``plugin_dir`` is the plugin root; schemas
    and patches live in its ``schemas/`` and ``patches/`` subdirectories."""

    plugin: str
    plugin_dir: Path

    @property
    def schemas_dir(self) -> Path:
        return self.plugin_dir / "schemas"

    @property
    def patches_dir(self) -> Path:
        return self.plugin_dir / "patches"

    def load_schemas(self) -> list[TableSchema]:
        return SchemaCompiler(self.schemas_dir).load_all()

    def load_patches(self) -> list[PatchDef]:
        return PatchCompiler(self.patches_dir).load_all()


# ── DB state snapshot ────────────────────────────────────────────────────────
@dataclass
class DbState:
    existing_tables: set[str]
    existing_columns: dict[str, set[str]]
    registry: dict[str, dict[str, RegistryEntry]]  # table -> fld_id -> entry
    applied_patch_ids: set[str]


# ── Plan model ───────────────────────────────────────────────────────────────
@dataclass
class MigrationOp:
    description: str
    sql: list[str]
    destructive: bool = False
    transactional: bool = True  # set False only for stmts that refuse a txn
                                # (e.g. CREATE INDEX CONCURRENTLY — not emitted)


@dataclass
class MigrationPlan:
    ops: list[MigrationOp] = field(default_factory=list)
    tables_created: list[str] = field(default_factory=list)
    lint_issues: list[LintIssue] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.ops

    @property
    def destructive_count(self) -> int:
        return sum(1 for op in self.ops if op.destructive)

    @property
    def has_errors(self) -> bool:
        return ddl_linter.has_errors(self.lint_issues)

    def all_statements(self) -> list[str]:
        out: list[str] = []
        for op in self.ops:
            out.extend(op.sql)
        return out


# ── 1. Read DB state ─────────────────────────────────────────────────────────
async def read_db_state(conn) -> DbState:
    from sqlalchemy import text

    rows = await conn.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'"
    ))
    existing_tables = {r[0] for r in rows}

    existing_columns: dict[str, set[str]] = {}
    rows = await conn.execute(text(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public'"
    ))
    for table_name, column_name in rows:
        existing_columns.setdefault(table_name, set()).add(column_name)

    registry: dict[str, dict[str, RegistryEntry]] = {}
    applied_patch_ids: set[str] = set()

    if "_field_registry" in existing_tables:
        # Only read columns that actually exist — self-healing for old installs.
        reg_cols = existing_columns.get("_field_registry", set())
        has_type    = "type" in reg_cols
        has_reqd    = "reqd" in reg_cols
        has_max     = "max_length" in reg_cols
        has_link    = "link_table" in reg_cols
        has_virtual = "is_virtual" in reg_cols
        select_cols = "table_name, fld_id, field_name"
        for col in ("type", "reqd", "max_length", "link_table", "is_virtual"):
            if col in reg_cols:
                select_cols += f", {col}"
        rows = await conn.execute(text(
            f"SELECT {select_cols} FROM _field_registry"
        ))
        for row in rows:
            r = row._mapping
            table_name = r["table_name"]
            fld_id     = r["fld_id"]
            registry.setdefault(table_name, {})[fld_id] = RegistryEntry(
                fld_id=fld_id,
                field_name=r["field_name"],
                type=r["type"] if has_type else "Data",
                reqd=bool(r["reqd"]) if has_reqd else False,
                max_length=r["max_length"] if has_max else None,
                link_table=r["link_table"] if has_link else None,
                is_virtual=bool(r["is_virtual"]) if has_virtual else False,
            )

    if "_patch_history" in existing_tables:
        rows = await conn.execute(text("SELECT patch_id FROM _patch_history"))
        applied_patch_ids = {r[0] for r in rows}

    return DbState(existing_tables, existing_columns, registry, applied_patch_ids)


# ── 2. Build plan (pure) ─────────────────────────────────────────────────────
def build_plan(
    sources: list[SchemaSource],
    state: DbState,
    *,
    confirm_destructive: bool,
) -> MigrationPlan:
    plan = MigrationPlan()

    # System objects first (uuid_generate_v7, arc_set_updated_at,
    # _field_registry, _patch_history, _trash).
    for stmt in SYSTEM_DDL:
        plan.ops.append(MigrationOp("system object", [stmt + (";" if not stmt.endswith(";") else "")]))

    all_schemas: list[TableSchema] = []
    all_patches: list[PatchDef] = []
    all_changes: list[ColumnChange] = []

    # ── Phase A: schemas from ALL sources (create new tables only) ───────────
    # Loading everything up front makes the plan independent of source order:
    # a patch onto a table created later in the same run is no longer skipped.
    for src in sources:
        for schema in src.load_schemas():
            all_schemas.append(schema)
            link_cols = [f.field_name for f in schema.fields if f.type == "Link"]
            if schema.table in state.existing_tables:
                # Table exists — modifications go through patches, but the
                # trigger/index infrastructure is ensured idempotently so
                # pre-existing installs are healed too.
                plan.ops.append(MigrationOp(
                    f"ensure infra {schema.table} ({src.plugin})",
                    table_infra_statements(schema.table, link_cols),
                ))
                continue
            sql = [compile_create_table(schema)]
            for fd in schema.fields:
                sql.append(registry_upsert(schema.plugin, schema.table, fd))
            sql.extend(table_infra_statements(schema.table, link_cols))
            plan.ops.append(MigrationOp(
                f"CREATE TABLE {schema.table} ({src.plugin})", sql
            ))
            plan.tables_created.append(schema.table)

    # ── Phase B: patches from ALL sources (modify existing/just-created) ─────
    known = state.existing_tables | set(plan.tables_created)
    for src in sources:
        for patch in src.load_patches():
            if patch.patch_id in state.applied_patch_ids:
                continue
            all_patches.append(patch)
            if patch.table not in known:
                continue  # linter will warn (patch_table_missing)
            snapshot = state.registry.get(patch.table, {})
            cols = state.existing_columns.get(patch.table, set())
            changes = compute_changes(patch, snapshot, cols)
            all_changes.extend(changes)
            for change in changes:
                plan.ops.append(MigrationOp(
                    f"[{patch.patch_id}] {change.kind.name} {change.table}",
                    generate_sql(change),
                    destructive=change.destructive,
                ))
            plan.ops.append(MigrationOp(
                f"record patch {patch.patch_id}",
                [
                    "INSERT INTO _patch_history (patch_id, plugin, table_name, description) "
                    f"VALUES ({_q(patch.patch_id)}, {_q(patch.plugin)}, {_q(patch.table)}, "
                    f"{_q(patch.description)}) "
                    "ON CONFLICT (patch_id) DO NOTHING;"
                ],
            ))

    # ── 3. Lint ──────────────────────────────────────────────────────────────
    plan.lint_issues = ddl_linter.lint(
        schemas=all_schemas,
        patches=all_patches,
        changes=all_changes,
        existing_tables=state.existing_tables,
        applied_patch_ids=state.applied_patch_ids,
        confirm_destructive=confirm_destructive,
    )
    log.info(
        "arc.db.plan_built",
        ops=len(plan.ops),
        tables_created=len(plan.tables_created),
        destructive=plan.destructive_count,
        errors=plan.has_errors,
    )
    return plan


# ── 4. Execute ───────────────────────────────────────────────────────────────
async def _op_transaction(conn):
    """Best-effort asyncpg transaction wrapper for one MigrationOp.

    The connection arrives with AUTOCOMMIT isolation (one statement per
    execute). PostgreSQL DDL is transactional, so we open an explicit
    server-side transaction around each op via the raw asyncpg connection;
    statements issued through SQLAlchemy in between run inside it. If the
    driver is not asyncpg (test doubles), we return None and run unwrapped.
    """
    try:
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection
        return driver.transaction()
    except Exception:  # pragma: no cover — non-asyncpg connection
        return None


async def execute(plan: MigrationPlan, conn) -> dict[str, int]:
    from sqlalchemy import text

    executed = 0
    for op in plan.ops:
        tx = (await _op_transaction(conn)) if op.transactional else None
        if tx is not None:
            await tx.start()
        try:
            for stmt in op.sql:
                await conn.execute(text(stmt))
                executed += 1
        except Exception as exc:
            if tx is not None:
                try:
                    await tx.rollback()
                except Exception:  # pragma: no cover
                    pass
            log.error(
                "arc.db.migration_op_failed",
                op=op.description,
                error=str(exc),
            )
            raise
        else:
            if tx is not None:
                await tx.commit()
    log.info("arc.db.migration_applied", statements=executed)
    return {"executed": executed, "ops": len(plan.ops)}