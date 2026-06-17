"""
plugins.relay.discovery
=======================
Auto-discovery, run once at plugin contribute() time.

Two sources per business plugin:

  resources/*.json   declarative CRUD — PURE SCAN, NO IMPORT (compile-ahead).
                     Each declaration generates routes at
                         {base}/{plugin}/{resource}            GET (list) / POST (save)
                         {base}/{plugin}/{resource}/{id}       GET (get) / DELETE (rm)
                     Generated handlers call ``arc`` and therefore run hooks.

  routes/*.py        custom handlers — IMPORTED here so their module-level
  (or api/*.py)      @get/@post/@delete/@stream decorators self-register.
                     The no-import rule applies to the JSON declaration scan,
                     NOT to controlled handler import in this discovery pass.

Custom routes use ABSOLUTE paths. A custom path that collides with a generated
resource path is a fail-fast error at discovery.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Iterable

from arc.kernel.logger import get_logger

log = get_logger("arc.plugin.relay.discovery")

_HANDLER_DIRS = ("routes", "api")


class DiscoveryError(RuntimeError):
    """Raised at discovery for malformed declarations or route collisions."""


def _resource_routes(base: str, module_prefix: str, decl: dict, arc) -> list[tuple]:
    """Build (verb, path, handler, roles) tuples for one resource declaration.

    ``module_prefix`` is the full import path (e.g. ``"plugins.hrms"``); only
    the last segment is used as the URL slug (``"hrms"``).
    """
    table = decl["table"]
    resource = decl.get("resource") or table.lower()
    fields = decl.get("fields")
    roles = tuple(decl.get("roles", ()))
    plugin_slug = module_prefix.split(".")[-1]   # "plugins.hrms" → "hrms"
    coll = f"{base}/{plugin_slug}/{resource}"
    item = f"{coll}/{{id}}"

    async def _list(ctx):
        return await arc.list(table, fields=fields, filters=ctx.query or None)

    async def _get(ctx):
        row = await arc.get(table, {"id": ctx.params["id"]})
        return row if row is not None else {"error": {"source": "db",
                "code": "not_found", "message": f"{resource} not found", "status": 404}}

    async def _save(ctx):
        return await arc.save(table, ctx.data)

    async def _rm(ctx):
        return await arc.rm(table, {"id": ctx.params["id"]})

    return [
        ("get", coll, _list, roles),
        ("get", item, _get, roles),
        ("post", coll, _save, roles),
        ("delete", item, _rm, roles),
    ]


def discover(registrar, arc, plugin_roots: Iterable[tuple[str, str]], *,
             base_path: str = "/api/v1") -> None:
    """Scan + import for each (module_prefix, root_path). Fail-fast on collisions.

    ``module_prefix`` is the importable Python path prefix, e.g. ``"plugins.hrms"``.
    The URL slug is always the last segment (``"hrms"``).
    """
    base = base_path.rstrip("/")

    # 1) Resource declarations (scan only — never import the JSON's table module).
    resource_paths: set[tuple[str, str]] = set()
    for module_prefix, root in plugin_roots:
        rdir = Path(root) / "resources"
        if not rdir.is_dir():
            continue
        for jf in sorted(rdir.glob("*.json")):
            try:
                decl = json.loads(jf.read_text())
            except ValueError as exc:
                raise DiscoveryError(f"{jf}: invalid JSON ({exc})") from exc
            if "table" not in decl:
                raise DiscoveryError(f"{jf}: resource declaration needs a 'table'.")
            slug = module_prefix.split(".")[-1]
            for verb, path, handler, roles in _resource_routes(base, module_prefix, decl, arc):
                method = "DELETE" if verb == "delete" else verb.upper()
                resource_paths.add((method, path))
                getattr(registrar, verb)(path, roles=roles, source=f"{slug}:resource")(handler)
            log.info("arc.relay.resource_discovered", plugin=slug,
                     resource=decl.get("resource") or decl["table"])

    # 2) Custom handler modules (import so module-level decorators fire).
    for module_prefix, root in plugin_roots:
        for sub in _HANDLER_DIRS:
            hdir = Path(root) / sub
            if not hdir.is_dir():
                continue
            for pf in sorted(hdir.glob("*.py")):
                if pf.name.startswith("_"):
                    continue
                module = f"{module_prefix}.{sub}.{pf.stem}"
                try:
                    importlib.import_module(module)
                    log.info("arc.relay.handlers_imported", module=module)
                except Exception as exc:   # surfaced, not swallowed
                    raise DiscoveryError(f"failed importing {module}: {exc}") from exc

    # 3) Collision check — a CUSTOM route (source not tagged ":resource") whose
    #    (method, path) duplicates a generated resource route. Distinguishing by
    #    source is required because both RouteSpecs share the same (method, path)
    #    tuple, so a set difference would mask the clash.
    clashes = sorted(
        (m, s.path)
        for s in registrar.routes
        if not s.source.endswith(":resource")
        for m in s.methods
        if (m, s.path) in resource_paths
    )
    if clashes:
        pretty = ", ".join(f"{m} {p}" for m, p in clashes)
        raise DiscoveryError(
            f"custom route(s) collide with auto-generated resource routes: {pretty}")


__all__ = ["discover", "DiscoveryError"]