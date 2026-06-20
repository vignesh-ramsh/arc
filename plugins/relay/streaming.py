"""
plugins.relay.streaming
=======================
Progress-streaming variants of the bulk operations, for long-running work
(e.g. 1000-row imports / exports / bulk updates) where the client wants
incremental progress rather than one response at the end.

These are async generators that yield progress dicts. They are surfaced on
``arc`` (ARC_SURFACE) and meant to be used inside an ``@stream`` route, whose
NDJSON transport already exists in relay — each yielded dict becomes one NDJSON
line:

    @stream(route="/employees/import", method="POST")
    async def import_employees(ctx):
        async for progress in arc.import_streamed("Employee", ctx.data["rows"]):
            yield progress

Progress shape:
    {"phase": "writing", "done": 230, "total": 1000, "errors": 2}
    {"phase": "complete", "done": 1000, "total": 1000, "errors": [...]}

Implementation note: these compose the EXISTING public ``arc`` methods
(``save``, ``update``, ``rm``, ``list_page``) in chunks — they do not reach into
documents.py internals. The non-streamed originals (save_many/update_many/
rm_many) are unchanged; this is purely additive. Has no dependency on redix.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable

from arc.kernel.logger import get_logger

log = get_logger("arc.plugin.relay.streaming")

_DEFAULT_CHUNK = 50


def build_streaming(arc, *, chunk_size: int = _DEFAULT_CHUNK) -> dict[str, Callable]:
    """Return the ``{attr: async-generator-fn}`` map relay contributes to
    ARC_SURFACE for streaming bulk operations. *arc* is relay's document API."""

    async def save_many_streamed(table: str, rows: list[dict], *,
                                 match_on: list[str] | None = None,
                                 atomic: bool = False) -> AsyncIterator[dict]:
        """Upsert rows in chunks, yielding progress. With atomic=False a failed
        row is recorded and processing continues (partial import); errors are
        returned in the final 'complete' frame."""
        total = len(rows)
        done = 0
        errors: list[dict] = []
        for i in range(0, total, chunk_size):
            chunk = rows[i:i + chunk_size]
            for row in chunk:
                try:
                    await arc.save(table, row, match_on=match_on)
                except Exception as exc:  # noqa: BLE001
                    if atomic:
                        yield {"phase": "error", "done": done, "total": total,
                               "error": str(exc)}
                        raise
                    errors.append({"row_index": done, "error": str(exc)})
                done += 1
            yield {"phase": "writing", "done": done, "total": total,
                   "errors": len(errors)}
        yield {"phase": "complete", "done": done, "total": total, "errors": errors}

    async def update_many_streamed(table: str, updates: list[dict], *,
                                   match_field: str = "id") -> AsyncIterator[dict]:
        """Apply per-row updates (each dict must carry *match_field*), chunked,
        yielding progress."""
        total = len(updates)
        done = 0
        errors: list[dict] = []
        for i in range(0, total, chunk_size):
            for row in updates[i:i + chunk_size]:
                key = row.get(match_field)
                try:
                    await arc.update(table, {match_field: key}, row)
                except Exception as exc:  # noqa: BLE001
                    errors.append({match_field: key, "error": str(exc)})
                done += 1
            yield {"phase": "writing", "done": done, "total": total,
                   "errors": len(errors)}
        yield {"phase": "complete", "done": done, "total": total, "errors": errors}

    async def rm_many_streamed(table: str, ids: list, *,
                               match_field: str = "id") -> AsyncIterator[dict]:
        """Soft-delete by id in chunks, yielding progress."""
        total = len(ids)
        done = 0
        errors: list[dict] = []
        for i in range(0, total, chunk_size):
            for _id in ids[i:i + chunk_size]:
                try:
                    await arc.rm(table, {match_field: _id})
                except Exception as exc:  # noqa: BLE001
                    errors.append({match_field: _id, "error": str(exc)})
                done += 1
            yield {"phase": "deleting", "done": done, "total": total,
                   "errors": len(errors)}
        yield {"phase": "complete", "done": done, "total": total, "errors": errors}

    async def import_streamed(table: str, rows: list[dict], *,
                              match_on: list[str] | None = None,
                              atomic: bool = False) -> AsyncIterator[dict]:
        """Alias of save_many_streamed under import vocabulary."""
        async for frame in save_many_streamed(table, rows, match_on=match_on,
                                              atomic=atomic):
            yield frame

    async def export_streamed(table: str, *, filters: dict | None = None,
                              fields: list[str] | None = None,
                              page_size: int | None = None) -> AsyncIterator[dict]:
        """Stream every matching row by paging arc.list_page (cursor mode) until
        exhausted. Each yielded frame carries a batch of rows plus running
        progress; callers serialize frames to NDJSON."""
        limit = page_size or chunk_size
        cursor = None
        sent = 0
        while True:
            page = await arc.list_page(table, fields=fields, filters=filters,
                                       cursor=cursor, limit=limit)
            rows = page.get("data") if isinstance(page, dict) else page
            rows = rows or []
            if not rows:
                break
            sent += len(rows)
            yield {"phase": "rows", "count": len(rows), "sent": sent, "data": rows}
            # Cursor pagination: stop when a short page comes back.
            cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if cursor is None or len(rows) < limit:
                break
        yield {"phase": "complete", "sent": sent}

    return {
        "save_many_streamed": save_many_streamed,
        "update_many_streamed": update_many_streamed,
        "rm_many_streamed": rm_many_streamed,
        "import_streamed": import_streamed,
        "export_streamed": export_streamed,
    }