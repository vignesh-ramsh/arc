"""
arc.plugins.psqldb.migrations.patch_compiler
======================================
Patches modify EXISTING tables — they never create or drop tables. Each patch
declares the desired fields; the compiler diffs them against _field_registry
(the source of truth) AND the live column set, and emits the minimal DDL plus
the registry-sync SQL.

Virtual fields (``type="Table"`` etc.) have no physical column. They are still
diffed against the registry — ADD writes a registry row, DROP deletes one —
but no ALTER TABLE / trash-capture SQL is emitted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from arc.kernel.exceptions import ArcError
from plugins.psqldb.migrations.schema import FieldDef, render_column_type

# patch_id is embedded in SQL and file names — keep it to a safe charset.
PATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


# ── Registry snapshot entry ─────────────────────────────────────────────────
@dataclass(frozen=True)
class RegistryEntry:
    fld_id: str
    field_name: str
    type: str
    reqd: bool
    max_length: int | None
    link_table: str | None = None
    is_virtual: bool = False


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
    def _valid_patch_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("patch_id must not be empty.")
        if not PATCH_ID.match(v):
            raise ValueError(
                f"patch_id '{v}' must match [A-Za-z0-9][A-Za-z0-9_.-]* "
                f"(max 128 chars) — it is recorded in SQL and history."
            )
        return v

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
    DROP_AND_ADD = auto()      # retained for compatibility; no longer emitted
    DROP_COLUMN = auto()
    SYNC_REGISTRY = auto()


@dataclass
class ColumnChange:
    kind: ChangeKind
    table: str
    plugin: str
    fld_id: str
    field_def: FieldDef | None = None
    old_field_name: str | None = None
    old_type: str | None = None
    old_reqd: bool | None = None
    old_max_length: int | None = None

    @property
    def destructive(self) -> bool:
        # Virtual-field drops only remove a registry row — never destructive.
        if self.kind is ChangeKind.DROP_COLUMN and self.field_def is None \
                and self.old_type in ("Table",):
            return False
        return self.kind in (ChangeKind.DROP_AND_ADD, ChangeKind.DROP_COLUMN)


# ── SQL escaping helpers ─────────────────────────────────────────────────────
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
    """INSERT-or-UPDATE one _field_registry row. Carries link_table for Link
    and Table types, and is_virtual=true for Table (no physical column)."""
    return (
        "INSERT INTO _field_registry "
        "(fld_id, table_name, field_name, type, reqd, max_length, "
        "link_table, is_virtual, plugin) VALUES ("
        f"{_q(field.fld_id)}, {_q(table)}, {_q(field.field_name)}, {_q(field.type)}, "
        f"{_null_or(field.reqd)}, {_null_or(field.max_length)}, "
        f"{_null_or(field.link_table)}, {_null_or(bool(field.is_virtual))}, "
        f"{_q(plugin)}) "
        "ON CONFLICT (fld_id, table_name) DO UPDATE SET "
        "field_name = EXCLUDED.field_name, type = EXCLUDED.type, "
        "reqd = EXCLUDED.reqd, max_length = EXCLUDED.max_length, "
        "link_table = EXCLUDED.link_table, is_virtual = EXCLUDED.is_virtual, "
        "updated_at = now();"
    )


def registry_delete(table: str, fld_id: str) -> str:
    return (
        f"DELETE FROM _field_registry WHERE table_name = {_q(table)} "
        f"AND fld_id = {_q(fld_id)};"
    )


# ── Column-drop trash capture ────────────────────────────────────────────────
def build_column_trash_capture(
    table: str, column: str, fld_id: str, type_name: str, plugin: str,
    reqd: bool | None = None, max_length: int | None = None,
) -> str:
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
    Virtual-field ADD/DROP touch the registry only — no ALTER TABLE."""
    t = change.table
    fd = change.field_def
    stmts: list[str] = []

    if change.kind is ChangeKind.ADD_COLUMN:
        assert fd is not None
        if fd.is_virtual:
            stmts.append(registry_upsert(change.plugin, t, fd))
            return stmts
        uniq = " UNIQUE" if fd.unique else ""
        stmts.append(
            f'ALTER TABLE "{t}" ADD COLUMN IF NOT EXISTS '
            f'"{fd.field_name}" {fd.column_type()} NULL{uniq};'
        )
        stmts.append(registry_upsert(change.plugin, t, fd))

    elif change.kind is ChangeKind.RENAME_COLUMN:
        assert fd is not None and change.old_field_name
        if fd.is_virtual:
            # Virtual field "renamed" → registry update only (no column to rename).
            stmts.append(registry_upsert(change.plugin, t, fd))
            return stmts
        stmts.append(
            f'ALTER TABLE "{t}" RENAME COLUMN '
            f'"{change.old_field_name}" TO "{fd.field_name}";'
        )
        stmts.append(registry_upsert(change.plugin, t, fd))

    elif change.kind is ChangeKind.ALTER_COLUMN:
        assert fd is not None
        if fd.is_virtual:
            stmts.append(registry_upsert(change.plugin, t, fd))
            return stmts
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

    elif change.kind is ChangeKind.SYNC_REGISTRY:
        assert fd is not None
        stmts.append(registry_upsert(change.plugin, t, fd))

    elif change.kind is ChangeKind.DROP_COLUMN:
        assert change.old_field_name
        # Virtual-field drop: registry delete only, no column to trash/drop.
        if change.old_type in ("Table",):
            stmts.append(registry_delete(t, change.fld_id))
            return stmts
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
        uniq = " UNIQUE" if fd.unique else ""
        stmts.append(
            f'ALTER TABLE "{t}" ADD COLUMN IF NOT EXISTS '
            f'"{fd.field_name}" {fd.column_type()} NULL{uniq};'
        )
        stmts.append(registry_upsert(change.plugin, t, fd))

    return stmts


# ── Change detection ─────────────────────────────────────────────────────────
def compute_changes(
    patch: PatchDef,
    registry_snapshot: dict[str, RegistryEntry],
    existing_columns: set[str],
) -> list[ColumnChange]:
    """Diff a patch against the registry snapshot AND live columns for its table.
    Virtual fields are diffed by registry only (no live-column comparison)."""
    changes: list[ColumnChange] = []
    patch_fld_ids = {f.fld_id for f in patch.fields}

    # DROPS: registry fld_ids for this table absent from the patch.
    for fld_id, entry in registry_snapshot.items():
        if fld_id in patch_fld_ids:
            continue
        # Virtual entries drop without column-existence check.
        if entry.is_virtual or entry.field_name in existing_columns:
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

    # ADD / RENAME / ALTER / SYNC
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

        # Virtual ↔ physical type changes are a hard error — these are not
        # equivalent shapes and an ALTER cannot bridge them safely.
        if entry.is_virtual != fd.is_virtual:
            raise ArcError(
                f"Field '{fd.field_name}' in '{patch.table}' changes between "
                f"virtual ({entry.type}) and physical ({fd.type}); declare a "
                f"new fld_id instead.",
                code="arc.db.patch.virtual_physical_swap",
            )

        name_changed = entry.field_name != fd.field_name
        attrs_changed = (
            entry.type != fd.type
            or entry.reqd != fd.reqd
            or entry.max_length != fd.max_length
            or (entry.link_table or None) != (fd.link_table or None)
        )

        # Virtual fields skip the live-column existence checks.
        if fd.is_virtual:
            if name_changed or attrs_changed:
                changes.append(ColumnChange(
                    kind=ChangeKind.ALTER_COLUMN,
                    table=patch.table,
                    plugin=patch.plugin,
                    fld_id=fd.fld_id,
                    field_def=fd,
                    old_field_name=entry.field_name,
                    old_type=entry.type,
                    old_reqd=entry.reqd,
                    old_max_length=entry.max_length,
                ))
            continue

        old_in_db = entry.field_name in existing_columns
        new_in_db = fd.field_name in existing_columns

        def _alter() -> ColumnChange:
            return ColumnChange(
                kind=ChangeKind.ALTER_COLUMN,
                table=patch.table,
                plugin=patch.plugin,
                fld_id=fd.fld_id,
                field_def=fd,
                old_type=entry.type,
                old_reqd=entry.reqd,
                old_max_length=entry.max_length,
            )

        if name_changed and old_in_db:
            changes.append(ColumnChange(
                kind=ChangeKind.RENAME_COLUMN,
                table=patch.table,
                plugin=patch.plugin,
                fld_id=fd.fld_id,
                field_def=fd,
                old_field_name=entry.field_name,
            ))
            if attrs_changed:
                changes.append(_alter())

        elif name_changed and new_in_db:
            changes.append(ColumnChange(
                kind=ChangeKind.SYNC_REGISTRY,
                table=patch.table,
                plugin=patch.plugin,
                fld_id=fd.fld_id,
                field_def=fd,
                old_field_name=entry.field_name,
            ))
            if attrs_changed:
                changes.append(_alter())

        elif name_changed:
            changes.append(ColumnChange(
                kind=ChangeKind.ADD_COLUMN,
                table=patch.table,
                plugin=patch.plugin,
                fld_id=fd.fld_id,
                field_def=fd,
                old_field_name=entry.field_name,
            ))

        elif attrs_changed:
            if old_in_db:
                changes.append(_alter())
            else:
                changes.append(ColumnChange(
                    kind=ChangeKind.ADD_COLUMN,
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
        if not self._dir.is_dir():
            return []
        patches: list[PatchDef] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ArcError(
                    f"Invalid JSON in patch file '{path}': {exc}",
                    code="arc.db.patch.bad_json",
                ) from exc
            try:
                patches.append(PatchDef.model_validate(raw))
            except Exception as exc:
                raise ArcError(
                    f"Invalid patch definition in '{path}': {exc}",
                    code="arc.db.patch.invalid",
                ) from exc
        return patches