"""
plugins.admin.schema_io
=======================
Validate a field list from the Table Builder / Schema Editor and write it to
``plugins/<plugin>/schemas/<Table>.json`` on disk.

Validation here is STRUCTURAL only — the rules that are stable and documented
(fld_id format, type whitelist, unique business-key requirement, system-field
blocklist, Link needs link_table). The authoritative compile + DDL lint happens
when the operator clicks **Migrate**, which runs ``arc db migrate`` — admin does
not import psqldb's compiler, keeping plugin boundaries intact.

A write never issues DDL. It only produces the JSON file; the database is
untouched until a separate, explicit migrate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from plugins.admin.errors import AdminError

FLD_ID = re.compile(r"^[A-Z]{2}[0-9]{2}$")
TABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# Physical + virtual Arc field types (must match psqldb's schema compiler).
PHYSICAL_TYPES = {
    "Data", "Text", "Int", "Float", "Decimal", "Bool", "Date", "Datetime",
    "JSON", "Link", "Email", "Password",
}
VIRTUAL_TYPES = {"Table"}
ALL_TYPES = PHYSICAL_TYPES | VIRTUAL_TYPES
LINK_REQUIRED = {"Link", "Table"}
LEN_TYPES = {"Data", "Email"}

SYSTEM_FIELDS = {
    "id", "created_at", "updated_at", "created_by", "updated_by", "_state",
}


def _err(msg: str) -> AdminError:
    return AdminError(msg)


def validate(table: str, plugin: str, fields: list[dict]) -> list[dict]:
    """Validate and normalise a field list. Returns the cleaned field dicts.
    Raises AdminError (400) on the first structural problem found, accumulating
    all messages into one error so the UI can show them together."""
    errs: list[str] = []

    if not isinstance(table, str) or not TABLE_NAME.match(table or ""):
        errs.append("table name must match [A-Za-z][A-Za-z0-9_]*")
    if not isinstance(plugin, str) or not PLUGIN_NAME.match(plugin or ""):
        errs.append("plugin must be lowercase snake_case")
    if table.lower() in SYSTEM_FIELDS:
        errs.append("table name collides with a system field")
    if not isinstance(fields, list) or not fields:
        errs.append("at least one field is required")
        raise AdminError("; ".join(errs))

    cleaned: list[dict] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    has_unique = False

    for i, f in enumerate(fields, start=1):
        if not isinstance(f, dict):
            errs.append(f"field {i}: not an object")
            continue
        name = (f.get("field_name") or "").strip()
        ftype = (f.get("type") or "").strip()
        fld_id = (f.get("fld_id") or "").strip().upper()

        if not name:
            errs.append(f"field {i}: field_name is required")
        elif name in SYSTEM_FIELDS:
            errs.append(f'field {i}: "{name}" is a reserved system field')
        elif name in seen_names:
            errs.append(f'field {i}: duplicate field_name "{name}"')
        seen_names.add(name)

        if ftype not in ALL_TYPES:
            errs.append(f'field {i}: unknown type "{ftype}"')

        # fld_id required for physical fields; optional for virtual Table fields.
        if ftype not in VIRTUAL_TYPES:
            if not FLD_ID.match(fld_id):
                errs.append(f"field {i}: fld_id must match [A-Z]{{2}}[0-9]{{2}}")
            elif fld_id in seen_ids:
                errs.append(f'field {i}: duplicate fld_id "{fld_id}"')
            seen_ids.add(fld_id)

        link_table = (f.get("link_table") or "").strip()
        if ftype in LINK_REQUIRED and not link_table:
            errs.append(f"field {i}: {ftype} requires link_table")

        if bool(f.get("unique")):
            has_unique = True

        obj: dict = {"fld_id": fld_id, "field_name": name, "type": ftype}
        if bool(f.get("reqd")):
            obj["reqd"] = True
        if bool(f.get("unique")):
            obj["unique"] = True
        if ftype in LEN_TYPES:
            ml = f.get("max_length")
            obj["max_length"] = int(ml) if ml else 140
        if ftype in LINK_REQUIRED and link_table:
            obj["link_table"] = link_table
        cleaned.append(obj)

    if not has_unique:
        errs.append("at least one field must be unique (the business key)")

    if errs:
        raise AdminError("; ".join(errs))
    return cleaned


def write_schema(*, table: str, plugin: str, fields: list[dict],
                 project_root: Path) -> dict:
    """Validate, then write plugins/<plugin>/schemas/<Table>.json. Overwrites an
    existing file (full schema replace — schemas create, patches modify).
    Returns ``{path, table, plugin, fields, bytes}``."""
    cleaned = validate(table, plugin, fields)

    # project_root is the plugins/ directory itself (Path.cwd() when arc runs
    # from the plugins dir). Schema path is therefore:
    #   {project_root}/{plugin}/schemas/{Table}.json
    # NOT {project_root}/plugins/{plugin}/... — that would double the prefix.
    #
    # Path-traversal guard: both segments already matched strict regexes, but
    # resolve and confirm containment before writing.
    schemas_dir = (project_root / plugin / "schemas").resolve()
    root = project_root.resolve()
    if root not in schemas_dir.parents and schemas_dir != root:
        raise AdminError("resolved schema path escapes the project root")

    schemas_dir.mkdir(parents=True, exist_ok=True)
    target = schemas_dir / f"{table}.json"

    doc = {"table": table, "plugin": plugin, "fields": cleaned}
    payload = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    target.write_text(payload, encoding="utf-8")

    # Return a path relative to project_root for display in the UI
    try:
        rel = target.relative_to(project_root.resolve())
    except ValueError:
        rel = target
    return {
        "path": str(rel),
        "table": table,
        "plugin": plugin,
        "fields": cleaned,
        "bytes": len(payload.encode("utf-8")),
    }


__all__ = ["validate", "write_schema", "ALL_TYPES"]