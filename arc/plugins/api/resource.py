"""
arc.plugins.api.resource
======================
A ``Resource`` is a declarative description of a table exposed over REST. The
api plugin turns each one into auto-CRUD endpoints. Only whitelisted ``fields``
are ever read or written — nothing leaks by default.

Declared at module level in ``{plugin}/resources/*.py``::

    from arc.plugins.api import Resource

    employee = Resource(
        plugin="hr",
        table="Employee",
        fields=["employee_name", "date_of_joining", "department"],
        create_fields=["employee_name", "date_of_joining", "department"],
        page_size=50,
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Resource:
    plugin: str
    table: str
    fields: list[str]
    create_fields: list[str] = field(default_factory=list)
    page_size: int = 50
    max_page_size: int = 200

    @property
    def path(self) -> str:
        return f"/api/v1/{self.plugin}/{self.table}"

    @property
    def writable(self) -> list[str]:
        return self.create_fields or self.fields

    # Always-exposed system columns alongside whitelisted fields.
    @property
    def read_columns(self) -> list[str]:
        return ["id", *self.fields, "_state", "created_at", "updated_at"]