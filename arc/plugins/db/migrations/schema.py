"""
arc.plugins.db.migrations.schema
==============================
Compiles JSON schema files into validated ``TableSchema`` objects and the DDL
to create them. System fields are injected automatically and must not appear
in the JSON.

Schema JSON (schemas/Employee.json)::

    {
      "table": "Employee",
      "plugin": "hr",
      "fields": [
        {"fld_id": "AA01", "field_name": "employee_code",
         "type": "Data", "reqd": true, "max_length": 32, "unique": true},
        {"fld_id": "AA02", "field_name": "employee_name",
         "type": "Data", "reqd": true, "max_length": 140},
        {"fld_id": "AA03", "field_name": "date_of_joining",
         "type": "Date", "reqd": true}
      ]
    }

Rules:
  * ``fld_id`` is ``[A-Z]{2}[0-9]{2}``, unique per table, permanent — never reused.
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

# Arc field type -> Postgres column type.
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
    "Link": "UUID",  # FK to another table's id
}


def render_column_type(type_name: str, max_length: int | None) -> str:
    """Render the Postgres column type for an Arc field type."""
    if type_name == "Data":
        return f"VARCHAR({max_length or 140})"
    return TYPE_MAP[type_name]


class FieldDef(BaseModel):
    model_config = ConfigDict(frozen=True)
    fld_id: str
    field_name: str
    type: str
    reqd: bool = False
    unique: bool = False
    max_length: int | None = None
    link_table: str | None = None

    @field_validator("fld_id")
    @classmethod
    def _valid_fld_id(cls, v: str) -> str:
        if not FLD_ID.match(v):
            raise ValueError(f"fld_id '{v}' must match [A-Z]{{2}}[0-9]{{2}}.")
        return v

    @field_validator("field_name")
    @classmethod
    def _not_system(cls, v: str) -> str:
        if v in SYSTEM_FIELDS:
            raise ValueError(f"'{v}' is a system field and cannot be declared.")
        if not TABLE_NAME.match(v):
            raise ValueError(f"field_name '{v}' must be a valid SQL identifier.")
        return v

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in TYPE_MAP:
            raise ValueError(f"Unknown field type '{v}'. Known: {sorted(TYPE_MAP)}")
        return v

    def column_type(self) -> str:
        return render_column_type(self.type, self.max_length)

    def column_def(self) -> str:
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
        # Unique fld_ids within the table.
        seen: set[str] = set()
        for f in self.fields:
            if f.fld_id in seen:
                raise ValueError(f"Duplicate fld_id '{f.fld_id}' in '{self.table}'.")
            seen.add(f.fld_id)

        # At least one business unique key (id is not a substitute).
        if not any(f.unique for f in self.fields):
            raise ValueError(
                f"Table '{self.table}' must declare at least one field with "
                f'"unique": true (a business key). The framework-managed id '
                f"column does not count."
            )
        return self


# System columns emitted first, in deterministic order.
# id uses UUID v7 (time-ordered) via the uuid_generate_v7() function that
# `arc db migrate` installs — see migrations/system.py.
_SYSTEM_DDL = [
    '"id" UUID PRIMARY KEY DEFAULT uuid_generate_v7()',
    '"created_at" TIMESTAMPTZ NOT NULL DEFAULT now()',
    '"updated_at" TIMESTAMPTZ NOT NULL DEFAULT now()',
    '"created_by" VARCHAR(255)',
    '"updated_by" VARCHAR(255)',
    '"_state" INTEGER NOT NULL DEFAULT 0',
]


def compile_create_table(schema: TableSchema) -> str:
    cols = list(_SYSTEM_DDL) + [f.column_def() for f in schema.fields]
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