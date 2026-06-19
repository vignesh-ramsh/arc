"""
arc.plugins.psqldb.migrations.schema
==============================
Compiles JSON schema files into validated ``TableSchema`` objects and the DDL
to create them. System fields are injected automatically and must not appear
in the JSON.

Field types
-----------
Physical columns:
    Data      VARCHAR(n)         business strings
    Text      TEXT               unbounded
    Int       INTEGER
    Float     DOUBLE PRECISION
    Decimal   NUMERIC
    Bool      BOOLEAN
    Date      DATE
    Datetime  TIMESTAMPTZ        always stored UTC
    JSON      JSONB
    Link      UUID               FK to another table's id (pair with link_table)
    Email     VARCHAR(n)         format-validated by relay before write
    Password  VARCHAR(255)       stored as written; STRIPPED from every HTTP response

Metadata-only (no physical column):
    Table     —                  declares "this row owns rows in another table"
                                 (pair with link_table). Recorded in
                                 _field_registry with is_virtual=true. Used by
                                 relay to cascade-soft-delete child rows when
                                 the parent is deleted.

Rules
-----
  * ``fld_id`` is ``[A-Z]{2}[0-9]{2}``, unique per table, permanent.
    OPTIONAL for ``Table``-type fields (Arc generates an internal reference
    key for them); REQUIRED for every physical field.
  * ``id`` (UUID v7) is the physical primary key, framework-managed.
  * Every schema MUST declare at least one field with ``"unique": true`` —
    a business key (employee_code, sku, email). ``id`` is not a substitute.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from arc.kernel.exceptions import ArcError

FLD_ID = re.compile(r"^[A-Z]{2}[0-9]{2}$")
TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# Reserved system field names — rejected if seen in JSON.
SYSTEM_FIELDS = {
    "id", "created_at", "updated_at", "created_by", "updated_by", "_state",
}

# Arc field type -> Postgres column type. (Virtual types are NOT in this map;
# they have no physical column.)
TYPE_MAP = {
    "Data": "VARCHAR",
    "Text": "TEXT",
    "Int": "INTEGER",
    "Float": "DOUBLE PRECISION",
    "Decimal": "NUMERIC",
    "Bool": "BOOLEAN",
    "Date": "DATE",
    "Datetime": "TIMESTAMPTZ",
    "JSON": "JSONB",
    "Link": "UUID",          # FK to another table's id (link_table required)
    "Email": "VARCHAR",      # format-validated by relay
    "Password": "VARCHAR",   # stripped from responses by relay
}

# Virtual types: declared in JSON, recorded in _field_registry with
# is_virtual=true, NEVER emit DDL. ``Table`` declares a parent→child
# relationship used by relay for cascade soft-delete.
VIRTUAL_TYPES = frozenset({"Table"})

ALL_TYPES = set(TYPE_MAP) | VIRTUAL_TYPES

# Types that require ``link_table`` to be set.
LINK_TYPES = frozenset({"Link", "Table"})

# Default max_length per stringy type when JSON omits it.
_DEFAULT_LENGTH = {"Data": 140, "Email": 254, "Password": 255}


def render_column_type(type_name: str, max_length: int | None) -> str:
    """Render the Postgres column type for an Arc field type.
    Caller must not pass a VIRTUAL_TYPES name — those have no column."""
    if type_name in VIRTUAL_TYPES:
        raise ValueError(f"render_column_type called for virtual type {type_name!r}")
    if type_name in _DEFAULT_LENGTH:
        return f"VARCHAR({max_length or _DEFAULT_LENGTH[type_name]})"
    return TYPE_MAP[type_name]


class FieldDef(BaseModel):
    model_config = ConfigDict(frozen=True)
    fld_id: str = ""               # may be empty for virtual fields (filled by validator)
    field_name: str
    type: str
    reqd: bool = False
    unique: bool = False
    max_length: int | None = None
    link_table: str | None = None

    @property
    def is_virtual(self) -> bool:
        return self.type in VIRTUAL_TYPES

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in ALL_TYPES:
            raise ValueError(f"Unknown field type '{v}'. Known: {sorted(ALL_TYPES)}")
        return v

    @field_validator("field_name")
    @classmethod
    def _not_system(cls, v: str) -> str:
        if v in SYSTEM_FIELDS:
            raise ValueError(f"'{v}' is a system field and cannot be declared.")
        if not TABLE_NAME.match(v):
            raise ValueError(f"field_name '{v}' must be a valid SQL identifier.")
        return v

    @model_validator(mode="after")
    def _validate_self(self) -> "FieldDef":
        # fld_id: required for physical fields, generated for virtual ones.
        if self.is_virtual:
            if not self.fld_id:
                # Arc-internal reference id for virtual fields. Not an [A-Z]{2}[0-9]{2}
                # code on purpose — it's not user-visible and cannot collide with one.
                object.__setattr__(self, "fld_id", f"_v_{self.field_name}")
            # Virtual fields cannot be reqd / unique — there's no column to enforce.
            if self.reqd or self.unique:
                raise ValueError(
                    f"'{self.field_name}' is a virtual {self.type} field; "
                    f"reqd/unique are not supported (no physical column)."
                )
            if self.max_length is not None:
                raise ValueError(
                    f"'{self.field_name}' is a virtual {self.type} field; "
                    f"max_length is not applicable."
                )
        else:
            if not self.fld_id:
                raise ValueError(f"'{self.field_name}': fld_id is required.")
            if not FLD_ID.match(self.fld_id):
                raise ValueError(f"fld_id '{self.fld_id}' must match [A-Z]{{2}}[0-9]{{2}}.")

        # Link / Table both require link_table.
        if self.type in LINK_TYPES and not self.link_table:
            raise ValueError(
                f"'{self.field_name}' is type {self.type}; link_table is required."
            )
        if self.type not in LINK_TYPES and self.link_table:
            raise ValueError(
                f"'{self.field_name}' is type {self.type}; link_table is only "
                f"valid for {sorted(LINK_TYPES)}."
            )
        return self

    def column_type(self) -> str:
        return render_column_type(self.type, self.max_length)

    def column_def(self) -> str:
        """DDL fragment for the CREATE TABLE — only meaningful for physical fields."""
        if self.is_virtual:
            raise ValueError(f"column_def called for virtual field {self.field_name!r}")
        parts = [f'"{self.field_name}"', self.column_type()]
        parts.append("NOT NULL" if self.reqd else "NULL")
        if self.unique:
            parts.append("UNIQUE")
        return " ".join(parts)


class TableSchema(BaseModel):
    model_config = ConfigDict(frozen=True)
    table: str
    plugin: str
    fields: list[FieldDef]

    @field_validator("table")
    @classmethod
    def _valid_table(cls, v: str) -> str:
        if not TABLE_NAME.match(v):
            raise ValueError(f"Table '{v}' must start with a letter (letters/digits/_).")
        return v

    @model_validator(mode="after")
    def _validate_fields(self) -> "TableSchema":
        # Unique fld_ids within the table (incl. generated virtual ones).
        seen: set[str] = set()
        for f in self.fields:
            if f.fld_id in seen:
                raise ValueError(f"Duplicate fld_id '{f.fld_id}' in '{self.table}'.")
            seen.add(f.fld_id)

        # At least one business unique key on a PHYSICAL field (id doesn't count,
        # virtuals can't be unique).
        if not any(f.unique for f in self.fields if not f.is_virtual):
            raise ValueError(
                f"Table '{self.table}' must declare at least one physical field "
                f'with "unique": true (a business key). The framework-managed '
                f"id column does not count."
            )
        return self

    @property
    def physical_fields(self) -> list[FieldDef]:
        return [f for f in self.fields if not f.is_virtual]

    @property
    def virtual_fields(self) -> list[FieldDef]:
        return [f for f in self.fields if f.is_virtual]


# System columns emitted first, in deterministic order.
_SYSTEM_DDL = [
    '"id" UUID PRIMARY KEY DEFAULT uuid_generate_v7()',
    '"created_at" TIMESTAMPTZ NOT NULL DEFAULT now()',
    '"updated_at" TIMESTAMPTZ NOT NULL DEFAULT now()',
    '"created_by" VARCHAR(255)',
    '"updated_by" VARCHAR(255)',
    '"_state" INTEGER NOT NULL DEFAULT 0',
]


def compile_create_table(schema: TableSchema) -> str:
    # Only physical fields produce columns; virtual fields are pure metadata.
    cols = list(_SYSTEM_DDL) + [f.column_def() for f in schema.physical_fields]
    body = ",\n  ".join(cols)
    return f'CREATE TABLE IF NOT EXISTS "{schema.table}" (\n  {body}\n);'


class SchemaCompiler:
    """Loads schema JSON from a plugin's ``schemas/`` directory."""

    def __init__(self, schemas_dir: Path) -> None:
        self._dir = schemas_dir

    def load_all(self) -> list[TableSchema]:
        if not self._dir.exists():
            return []
        return [self._load(p) for p in sorted(self._dir.glob("*.json"))]

    @staticmethod
    def _load(path: Path) -> TableSchema:
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ArcError(
                f"Schema '{path}' is not valid JSON: {exc}",
                code="arc.db.schema.invalid_json",
            ) from exc
        try:
            return TableSchema.model_validate(raw)
        except Exception as exc:
            raise ArcError(
                f"Schema '{path}' failed validation: {exc}",
                code="arc.db.schema.invalid",
            ) from exc