"""
arc.plugins.db.migrations.ddl_linter
==================================
Pre-flight safety checks. Pure functions over compiled schemas, patches, and
computed changes — no database access. The migrator runs the linter after
building the plan and BEFORE executing any DDL. Any ERROR blocks the migration;
the plan is printed but nothing is applied.

Checks
------
ERROR
  * duplicate table name across plugins (fast fail)
  * system field name declared in a schema or patch
  * destructive change without --confirm-destructive
  * the same (table, fld_id) declared by more than one schema/patch —
    fld_id is the registry key, so a collision makes one patch silently
    rewrite another plugin's column

WARNING
  * patch already applied (will be skipped)
  * patch targets a missing table (will be skipped)
  * ALTER changes the column TYPE — the USING cast can fail or truncate on
    incompatible existing data
  * ALTER adds SET NOT NULL — fails if the column currently contains NULLs
  * ADD COLUMN with reqd=true on an existing table — the column is created
    nullable (a populated table cannot take NOT NULL without a default)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from arc.plugins.db.migrations.patch_compiler import ChangeKind, ColumnChange, PatchDef
from arc.plugins.db.migrations.schema import SYSTEM_FIELDS, TableSchema


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class LintIssue:
    severity: Severity
    code: str
    message: str
    location: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR

    def __str__(self) -> str:
        loc = f"  [{self.location}]" if self.location else ""
        return f"{self.severity.value.upper()}: {self.message}{loc}"


def lint(
    *,
    schemas: Sequence[TableSchema],
    patches: Sequence[PatchDef],
    changes: Sequence[ColumnChange],
    existing_tables: set[str],
    applied_patch_ids: set[str],
    confirm_destructive: bool,
) -> list[LintIssue]:
    issues: list[LintIssue] = []

    # 1. Duplicate table names across plugins → fast fail.
    seen: dict[str, str] = {}
    for s in schemas:
        if s.table in seen and seen[s.table] != s.plugin:
            issues.append(LintIssue(
                Severity.ERROR,
                "arc.db.lint.duplicate_table",
                f"Table '{s.table}' is defined by both '{seen[s.table]}' and "
                f"'{s.plugin}'. Table names must be unique across all plugins.",
                location=s.plugin,
            ))
        else:
            seen[s.table] = s.plugin

    # 2. System field names in schemas/patches (defensive — also validated earlier).
    for s in schemas:
        for f in s.fields:
            if f.field_name in SYSTEM_FIELDS:
                issues.append(LintIssue(
                    Severity.ERROR,
                    "arc.db.lint.system_field",
                    f"'{f.field_name}' is a system field and cannot be declared.",
                    location=f"{s.plugin}/schemas/{s.table}",
                ))
    for p in patches:
        for f in p.fields:
            if f.field_name in SYSTEM_FIELDS:
                issues.append(LintIssue(
                    Severity.ERROR,
                    "arc.db.lint.system_field",
                    f"'{f.field_name}' is a system field and cannot be patched.",
                    location=f"{p.plugin}/patches/{p.patch_id}",
                ))

    # 3. fld_id collisions per table across all schemas + patches.
    #    _field_registry is keyed (fld_id, table_name) — two declarations of
    #    the same pair from different sources would silently fight over one
    #    registry row and rewrite each other's column.
    owners: dict[tuple[str, str], str] = {}
    for s in schemas:
        for f in s.fields:
            owners[(s.table, f.fld_id)] = f"{s.plugin}/schemas/{s.table}"
    for p in patches:
        for f in p.fields:
            key = (p.table, f.fld_id)
            here = f"{p.plugin}/patches/{p.patch_id}"
            if key in owners:
                issues.append(LintIssue(
                    Severity.ERROR,
                    "arc.db.lint.fld_id_collision",
                    f"fld_id '{f.fld_id}' on table '{p.table}' is declared by "
                    f"both '{owners[key]}' and '{here}'. fld_id must be unique "
                    f"per table across every schema and patch.",
                    location=here,
                ))
            else:
                owners[key] = here

    # 4. Patch hygiene.
    known_tables = set(existing_tables) | {s.table for s in schemas}
    for p in patches:
        if p.patch_id in applied_patch_ids:
            issues.append(LintIssue(
                Severity.WARNING,
                "arc.db.lint.patch_already_applied",
                f"Patch '{p.patch_id}' already applied — skipping.",
                location=p.plugin,
            ))
        if p.table not in known_tables:
            issues.append(LintIssue(
                Severity.WARNING,
                "arc.db.lint.patch_table_missing",
                f"Patch '{p.patch_id}' targets table '{p.table}', which does not "
                f"exist — skipping.",
                location=p.plugin,
            ))

    # 5. Destructive changes require confirmation.
    if not confirm_destructive:
        for c in changes:
            if c.destructive:
                op = "DROP COLUMN" if c.kind is ChangeKind.DROP_COLUMN else "DROP+ADD"
                col = c.old_field_name or (c.field_def.field_name if c.field_def else "")
                issues.append(LintIssue(
                    Severity.ERROR,
                    "arc.db.lint.destructive",
                    f"{op} on '{c.table}'.'{col}' is destructive. "
                    f"Re-run with --confirm-destructive to proceed.",
                    location=c.plugin,
                ))

    # 6. Risky-but-allowed ALTERs — warn so failures at execute time are
    #    never a surprise. PostgreSQL DDL is transactional, so a failed ALTER
    #    rolls its op back cleanly, but the operator should know in advance.
    for c in changes:
        if c.kind is not ChangeKind.ALTER_COLUMN or c.field_def is None:
            continue
        fd = c.field_def
        if c.old_type is not None and c.old_type != fd.type:
            issues.append(LintIssue(
                Severity.WARNING,
                "arc.db.lint.type_change",
                f"ALTER on '{c.table}'.'{fd.field_name}' changes type "
                f"{c.old_type} → {fd.type}. The USING cast fails if existing "
                f"data is incompatible (the op rolls back; nothing is lost).",
                location=c.plugin,
            ))
        if fd.reqd and c.old_reqd is False:
            issues.append(LintIssue(
                Severity.WARNING,
                "arc.db.lint.set_not_null",
                f"ALTER on '{c.table}'.'{fd.field_name}' adds NOT NULL — this "
                f"fails if the column currently contains NULLs. Backfill first.",
                location=c.plugin,
            ))

    # 7. reqd on ADD COLUMN is not enforced (added nullable on purpose).
    for c in changes:
        if c.kind is ChangeKind.ADD_COLUMN and c.field_def is not None and c.field_def.reqd:
            issues.append(LintIssue(
                Severity.WARNING,
                "arc.db.lint.reqd_not_enforced",
                f"'{c.table}'.'{c.field_def.field_name}' is declared reqd but is "
                f"added NULLable (existing rows have no value). Backfill, then "
                f"re-apply the patch attrs to enforce NOT NULL via ALTER.",
                location=c.plugin,
            ))

    return issues


def has_errors(issues: Sequence[LintIssue]) -> bool:
    return any(i.is_error for i in issues)