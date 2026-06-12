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
  * duplicate table name across plugins (point 5 — fast fail)
  * system field name declared in a schema or patch
  * a patch targets a table that does not (and will not) exist
        → downgraded to WARNING (skipped, not fatal)
  * destructive change without --confirm-destructive

WARNING
  * patch already applied (will be skipped)
  * patch targets a missing table (will be skipped)
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

    # 3. Patch hygiene.
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

    # 4. Destructive changes require confirmation.
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

    return issues


def has_errors(issues: Sequence[LintIssue]) -> bool:
    return any(i.is_error for i in issues)