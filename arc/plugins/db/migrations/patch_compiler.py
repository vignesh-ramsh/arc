"""
arc.plugins.db.migrations.patch_compiler
======================================
Patches modify EXISTING tables — they never create or drop tables. Each patch
declares the desired fields; the compiler diffs them against _field_registry
(the source of truth) and emits the minimal DDL plus the registry-sync SQL.

Field-change detection (the 5 operations)
-----------------------------------------
    fld_id not in registry                       → ADD COLUMN          (safe)
    fld_id exists, field_name changed, col there → RENAME COLUMN       (safe)
    fld_id exists, type/reqd/length changed      → ALTER COLUMN        (safe)
    fld_id exists, name changed, col missing     → DROP + ADD          (destructive)
    fld_id in registry, absent from patch        → DROP COLUMN         (destructive)

Registry sync (point 2): ADD inserts, RENAME/ALTER update, DROP deletes the
registry row. ``fld_id`` is the immutable key and is never altered.

Column drops (point 6): the dropped column's values are captured into _trash as
a single row with ``drop_type='column'`` before the DROP runs, so the data can
be recovered later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from arc.kernel.exceptions import ArcError
from arc.plugins.db.migrations.schema import FieldDef, render_column_type


# ── Registry snapshot entry ─────────────────────────────────────────────────
@dataclass(frozen=True)
class RegistryEntry:
    fld_id: str
    field_name: str
    type: str
    reqd: bool
    max_length: int | None


# ── PatchDef ─────────────────────────────────────────────────────────────────
class PatchDef(BaseModel):
    model_config = ConfigDict(frozen=True)
    table: str
    patch_id: str
    plugin: str
    description: str = ""
    fields: list[FieldDef]

    @field_validator("patch_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("patch_id must not be empty.")
        return v.strip()

    @model_validator(mode="after")
    def _unique_fld_ids(self) -> "PatchDef":
        seen: set[str] = set()
        for f in self.fields:
            if f.fld_id in seen:
                raise ValueError(
                    f"Duplicate fld_id '{f.fld_id}' in patch '{self.patch_id}'."
                )
            seen.add(f.fld_id)
        return self


# ── Change model ─────────────────────────────────────────────────────────────
class ChangeKind(Enum):
    ADD_COLUMN = auto()
    RENAME_COLUMN = auto()
    ALTER_COLUMN = auto()
    DROP_AND_ADD = auto()
    DROP_COLUMN = auto()


@dataclass
class ColumnChange:
    kind: ChangeKind
    table: str
    plugin: str
    fld_id: str
    field_def: FieldDef | None = None      # new/updated definition
    old_field_name: str | None = None      # for RENAME / DROP / DROP_AND_ADD
    old_type: str | None = None            # for column-drop trash capture
    old_reqd: bool | None = None           # for column-drop recovery
    old_max_length: int | None = None      # for column-drop recovery

    @property
    def destructive(self) -> bool:
        return self.kind in (ChangeKind.DROP_AND_ADD, ChangeKind.DROP_COLUMN)


# ── SQL escaping helper ──────────────────────────────────────────────────────
def _q(value: str) -> str:
    """Single-quote a SQL string literal, doubling embedded quotes."""
    return "'" + value.replace("'", "''") + "'"


def _null_or(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _q(str(value))


# ── Registry-sync SQL (shared with the migrator for CREATE TABLE) ────────────
def registry_upsert(plugin: str, table: str, field: FieldDef) -> str:
    return (
        "INSERT INTO _field_registry "
        "(fld_id, table_name, field_name, type, reqd, max_length, plugin) VALUES ("
        f"{_q(field.fld_id)}, {_q(table)}, {_q(field.field_name)}, {_q(field.type)}, "
        f"{_null_or(field.reqd)}, {_null_or(field.max_length)}, {_q(plugin)}) "
        "ON CONFLICT (fld_id, table_name) DO UPDATE SET "
        "field_name = EXCLUDED.field_name, type = EXCLUDED.type, "
        "reqd = EXCLUDED.reqd, max_length = EXCLUDED.max_length, updated_at = now();"
    )


def registry_delete(table: str, fld_id: str) -> str:
    return (
        f"DELETE FROM _field_registry WHERE table_name = {_q(table)} "
        f"AND fld_id = {_q(fld_id)};"
    )


# ── Column-drop trash capture (point 6) ──────────────────────────────────────
def build_column_trash_capture(
    table: str, column: str, fld_id: str, type_name: str, plugin: str,
    reqd: bool | None = None, max_length: int | None = None,
) -> str:
    """Capture every value of a column into a single _trash row before dropping."""
    return (
        "INSERT INTO _trash (table_name, record_id, drop_type, data, deleted_by) "
        f"SELECT {_q(table)}, NULL, 'column', jsonb_build_object("
        f"'table', {_q(table)}, 'column', {_q(column)}, 'fld_id', {_q(fld_id)}, "
        f"'type', {_q(type_name)}, 'reqd', {_null_or(bool(reqd))}, "
        f"'max_length', {_null_or(max_length)}, 'plugin', {_q(plugin)}, "
        "'values', COALESCE("
        f'jsonb_agg(jsonb_build_object(\'id\', id, \'value\', "{column}")), '
        "'[]'::jsonb)), NULL "
        f'FROM "{table}";'
    )


# ── DDL generation per change ────────────────────────────────────────────────
def generate_sql(change: ColumnChange) -> list[str]:
    """Return the ordered SQL statements for one change (incl. registry sync).

    For destructive drops, the trash-capture statement is included first.
    """
    t = change.table
    fd = change.field_def
    stmts: list[str] = []

    if change.kind is ChangeKind.ADD_COLUMN:
        assert fd is not None
        null = "NOT NULL" if fd.reqd else "NULL"
        uniq = " UNIQUE" if fd.unique else ""
        # NOT NULL on an existing table needs a default or the table to be empty.
        # We add nullable first when reqd to avoid failing on populated tables;
        # enforce reqd via a follow-up SET NOT NULL only when safe is out of scope here.
        col_null = "NULL" if fd.reqd else null
        stmts.append(
            f'ALTER TABLE "{t}" ADD COLUMN IF NOT EXISTS '
            f'"{fd.field_name}" {fd.column_type()} {col_null}{uniq};'
        )
        stmts.append(registry_upsert(change.plugin, t, fd))

    elif change.kind is ChangeKind.RENAME_COLUMN:
        assert fd is not None and change.old_field_name
        stmts.append(
            f'ALTER TABLE "{t}" RENAME COLUMN '
            f'"{change.old_field_name}" TO "{fd.field_name}";'
        )
        stmts.append(registry_upsert(change.plugin, t, fd))

    elif change.kind is ChangeKind.ALTER_COLUMN:
        assert fd is not None
        col_type = render_column_type(fd.type, fd.max_length)
        stmts.append(
            f'ALTER TABLE "{t}" ALTER COLUMN "{fd.field_name}" '
            f'TYPE {col_type} USING "{fd.field_name}"::{col_type};'
        )
        nullability = "SET NOT NULL" if fd.reqd else "DROP NOT NULL"
        stmts.append(
            f'ALTER TABLE "{t}" ALTER COLUMN "{fd.field_name}" {nullability};'
        )
        stmts.append(registry_upsert(change.plugin, t, fd))

    elif change.kind is ChangeKind.DROP_COLUMN:
        assert change.old_field_name
        stmts.append(
            build_column_trash_capture(
                t, change.old_field_name, change.fld_id,
                change.old_type or "", change.plugin,
                change.old_reqd, change.old_max_length,
            )
        )
        stmts.append(f'ALTER TABLE "{t}" DROP COLUMN IF EXISTS "{change.old_field_name}";')
        stmts.append(registry_delete(t, change.fld_id))

    elif change.kind is ChangeKind.DROP_AND_ADD:
        assert fd is not None and change.old_field_name
        stmts.append(
            build_column_trash_capture(
                t, change.old_field_name, change.fld_id,
                change.old_type or "", change.plugin,
                change.old_reqd, change.old_max_length,
            )
        )
        stmts.append(f'ALTER TABLE "{t}" DROP COLUMN IF EXISTS "{change.old_field_name}";')
        null = "NULL" if not fd.reqd else "NULL"  # add nullable; reqd enforced separately
        uniq = " UNIQUE" if fd.unique else ""
        stmts.append(
            f'ALTER TABLE "{t}" ADD COLUMN IF NOT EXISTS '
            f'"{fd.field_name}" {fd.column_type()} {null}{uniq};'
        )
        stmts.append(registry_upsert(change.plugin, t, fd))

    return stmts


# ── Change detection ─────────────────────────────────────────────────────────
def compute_changes(
    patch: PatchDef,
    registry_snapshot: dict[str, RegistryEntry],
    existing_columns: set[str],
) -> list[ColumnChange]:
    """Diff a patch against the registry snapshot for its table."""
    changes: list[ColumnChange] = []
    patch_fld_ids = {f.fld_id for f in patch.fields}

    # DROPS: registry fld_ids for this table absent from the patch.
    for fld_id, entry in registry_snapshot.items():
        if fld_id not in patch_fld_ids and entry.field_name in existing_columns:
            changes.append(ColumnChange(
                kind=ChangeKind.DROP_COLUMN,
                table=patch.table,
                plugin=patch.plugin,
                fld_id=fld_id,
                old_field_name=entry.field_name,
                old_type=entry.type,
                old_reqd=entry.reqd,
                old_max_length=entry.max_length,
            ))

    # ADD / RENAME / ALTER / DROP_AND_ADD
    for fd in patch.fields:
        entry = registry_snapshot.get(fd.fld_id)
        if entry is None:
            changes.append(ColumnChange(
                kind=ChangeKind.ADD_COLUMN,
                table=patch.table,
                plugin=patch.plugin,
                fld_id=fd.fld_id,
                field_def=fd,
            ))
            continue

        name_changed = entry.field_name != fd.field_name
        attrs_changed = (
            entry.type != fd.type
            or entry.reqd != fd.reqd
            or entry.max_length != fd.max_length
        )
        col_in_db = entry.field_name in existing_columns

        if name_changed and not col_in_db:
            changes.append(ColumnChange(
                kind=ChangeKind.DROP_AND_ADD,
                table=patch.table,
                plugin=patch.plugin,
                fld_id=fd.fld_id,
                field_def=fd,
                old_field_name=entry.field_name,
                old_type=entry.type,
                old_reqd=entry.reqd,
                old_max_length=entry.max_length,
            ))
        elif name_changed:
            changes.append(ColumnChange(
                kind=ChangeKind.RENAME_COLUMN,
                table=patch.table,
                plugin=patch.plugin,
                fld_id=fd.fld_id,
                field_def=fd,
                old_field_name=entry.field_name,
            ))
            if attrs_changed:
                changes.append(ColumnChange(
                    kind=ChangeKind.ALTER_COLUMN,
                    table=patch.table,
                    plugin=patch.plugin,
                    fld_id=fd.fld_id,
                    field_def=fd,
                ))
        elif attrs_changed:
            changes.append(ColumnChange(
                kind=ChangeKind.ALTER_COLUMN,
                table=patch.table,
                plugin=patch.plugin,
                fld_id=fd.fld_id,
                field_def=fd,
            ))

    return changes


# ── Loader ───────────────────────────────────────────────────────────────────
class PatchCompiler:
    """Loads and validates ``patches/*.json`` for a plugin."""

    def __init__(self, patches_dir: Path) -> None:
        self._dir = patches_dir

    def load_all(self) -> list[PatchDef]:
        if not self._dir.exists():
            return []
        return [self._load(p) for p in sorted(self._dir.glob("*.json"))]

    @staticmethod
    def _load(path: Path) -> PatchDef:
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ArcError(
                f"Patch '{path}' is not valid JSON: {exc}",
                code="arc.db.patch.invalid_json",
            ) from exc
        try:
            return PatchDef.model_validate(raw)
        except Exception as exc:
            raise ArcError(
                f"Patch '{path}' failed validation: {exc}",
                code="arc.db.patch.invalid",
            ) from exc