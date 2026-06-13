"""
arc.plugins.psqldb.migrations.patch_compiler
======================================
Patches modify EXISTING tables — they never create or drop tables. Each patch
declares the desired fields; the compiler diffs them against _field_registry
(the source of truth) AND the live column set, and emits the minimal DDL plus
the registry-sync SQL.

Field-change detection
----------------------
    fld_id not in registry                          → ADD COLUMN        (safe)
    fld_id exists, name changed, old column in DB   → RENAME COLUMN     (safe)
    fld_id exists, name changed, NEW column in DB   → SYNC_REGISTRY     (safe)
        (the rename already happened — e.g. a previous run applied the DDL but
         died before the registry sync; we heal the registry, never drop data)
    fld_id exists, name changed, neither col in DB  → ADD COLUMN        (safe)
        (drift: the column vanished outside Arc; recreate under the new name)
    fld_id exists, attrs changed, col in DB         → ALTER COLUMN      (safe*)
    fld_id exists, attrs changed, col missing       → ADD COLUMN        (safe)
    fld_id in registry, absent from patch, col in DB → DROP COLUMN  (destructive)

(*) Type changes and SET NOT NULL can still fail on incompatible data — the
DDL linter emits warnings for those so the operator is not surprised.

The old DROP_AND_ADD escalation (name changed + column missing → destructive
drop of a column that does not exist) is gone: it both generated DDL that
could not succeed (trash-capturing a non-existent column) and, worse, turned
a *safe rename interrupted mid-apply* into a destructive operation on re-run.

Registry sync: ADD/SYNC insert-or-update, RENAME/ALTER update, DROP deletes
the registry row. ``fld_id`` is the immutable key and is never altered.

Column drops: the dropped column's values are captured into _trash as a single
row with ``drop_type='column'`` before the DROP runs, so the data can be
recovered later.
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
from arc.plugins.psqldb.migrations.schema import FieldDef, render_column_type

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
    SYNC_REGISTRY = auto()     # DDL already applied; registry row needs healing


@dataclass
class ColumnChange:
    kind: ChangeKind
    table: str
    plugin: str
    fld_id: str
    field_def: FieldDef | None = None      # new/updated definition
    old_field_name: str | None = None      # for RENAME / DROP / DROP_AND_ADD
    old_type: str | None = None            # column-drop trash capture + lint
    old_reqd: bool | None = None           # column-drop recovery + lint
    old_max_length: int | None = None      # column-drop recovery + lint

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


# ── Column-drop trash capture ────────────────────────────────────────────────
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
        uniq = " UNIQUE" if fd.unique else ""
        # NOT NULL on an existing table needs a default or the table to be
        # empty. We add nullable first when reqd to avoid failing on populated
        # tables; the linter warns that reqd is not enforced for this column.
        stmts.append(
            f'ALTER TABLE "{t}" ADD COLUMN IF NOT EXISTS '
            f'"{fd.field_name}" {fd.column_type()} NULL{uniq};'
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

    elif change.kind is ChangeKind.SYNC_REGISTRY:
        # The DDL is already in effect (e.g. a rename applied on a previous
        # run that died before the registry write). Heal the registry only.
        assert fd is not None
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
        # No longer emitted by compute_changes; kept so externally constructed
        # plans (tests, tools) still execute. Trash capture only makes sense if
        # the old column actually exists — guard with to_regclass-safe DDL.
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
    """Diff a patch against the registry snapshot AND live columns for its table."""
    changes: list[ColumnChange] = []
    patch_fld_ids = {f.fld_id for f in patch.fields}

    # DROPS: registry fld_ids for this table absent from the patch.
    # Only when the column actually exists — dropping a phantom column would
    # capture nothing and only thrash the registry.
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

        name_changed = entry.field_name != fd.field_name
        attrs_changed = (
            entry.type != fd.type
            or entry.reqd != fd.reqd
            or entry.max_length != fd.max_length
        )
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
            # Rename already in effect on the DB side — heal the registry.
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
            # Neither old nor new column exists: the column drifted away
            # outside Arc. Recreate it under the new name — never a drop.
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
                # Column drifted away; ADD recreates it with the new attrs.
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