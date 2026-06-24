"""
plugins.admin.introspect
=========================
Live schema introspection over the ``_field_registry`` system table — the
source of truth for every column Arc has created. Reads run through the
read-only ``arc.query`` surface (single SELECT, parameterised). These power the
Schema Viewer's plugin/table tree and per-table field view.

The registry reflects the MIGRATED state, not the schema JSON files on disk —
which is exactly what you want to view and edit against.
"""

from __future__ import annotations

from plugins.relay import arc


async def list_tables() -> list[dict]:
    """Every Arc-managed table as ``{table_name, plugin, field_count}``,
    ordered by plugin then table. Virtual (no-column) field rows are excluded
    from the count but a table that exists only via virtual fields still shows."""
    rows = await arc.query(
        'SELECT table_name, plugin, '
        'COUNT(*) FILTER (WHERE is_virtual = false) AS field_count '
        'FROM _field_registry '
        'GROUP BY table_name, plugin '
        'ORDER BY plugin, table_name'
    )
    return [
        {"table_name": r["table_name"], "plugin": r["plugin"],
         "field_count": int(r["field_count"] or 0)}
        for r in rows
    ]


async def table_fields(table: str) -> list[dict]:
    """The field rows for one table, ordered by fld_id. Empty list if the table
    is not in the registry (caller turns that into a 404)."""
    rows = await arc.query(
        'SELECT fld_id, field_name, type, reqd, max_length, link_table, '
        'is_virtual, plugin '
        'FROM _field_registry WHERE table_name = :t '
        'ORDER BY fld_id',
        {"t": table},
    )
    return [
        {
            "fld_id": r["fld_id"],
            "field_name": r["field_name"],
            "type": r["type"],
            "reqd": bool(r["reqd"]),
            "max_length": r["max_length"],
            "link_table": r["link_table"],
            "is_virtual": bool(r["is_virtual"]),
            "plugin": r["plugin"],
        }
        for r in rows
    ]


__all__ = ["list_tables", "table_fields"]
