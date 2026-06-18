"""
plugins.relay.discovery
=======================
Auto-discovery, run once at plugin contribute() time.

Two sources per business plugin:

  resources/*.json   declarative CRUD — PURE SCAN, NO IMPORT (compile-ahead).
                     Generated handlers call ``arc`` and therefore run hooks.
  routes/*.py        custom handlers — IMPORTED so module-level
  (or api/*.py)      @get/@post/@patch/@delete/@stream decorators self-register.

Resource declaration (v2 — auto-CRUD is opt-in; only declared verbs generate routes):

  {
    "table": "Employee",
    "resource_name": "employees",
    "permissions": { "HR Manager": ["get","post","patch"],
                     "Super Admin": ["get","post","patch","delete"] },
    "upsert_keys": ["employee_id"],          # POST/PATCH match key (omit → id-or-insert)
    "fields": ["id","employee_name","department","updated_at"],
    "filters": {
      "mandatory": ["department"],            # 400 if absent from the querystring
      "static":    {"_state": {"ne": 99}},    # server-applied, non-overridable
      "optional":  {"date_of_joining": ["gte","lte"], "designation": ["eq","in"]}
    },
    "query": ["employee_name","employee_id"], # ?q= → ILIKE OR across these
    "limit": 100                              # default page size (clamped to list_cap)
  }

  Verb → route:
    get    →  GET {base}/{plugin}/{resource}        (list)
              GET {base}/{plugin}/{resource}/{id}   (get one → 404 NotFoundError)
    post   →  POST {base}/{plugin}/{resource}       (save = upsert by upsert_keys)
    patch  →  PATCH {base}/{plugin}/{resource}/{id} (update-existing-only)
    delete →  DELETE {base}/{plugin}/{resource}/{id}(soft delete)

Legacy declarations (no "permissions": uses flat "roles" + "resource") keep the
original 4-route GET/GET/POST/DELETE shape for back-compat.

Custom routes use ABSOLUTE paths. A custom path that collides with a generated
resource path is a fail-fast error at discovery.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Iterable

from arc.kernel.logger import get_logger

from plugins.relay.errors import BadParam, NotFoundError
from plugins.relay.filters import (
    CANON_OPS, allowed_ops_map, normalize_filters, parse_qs,
    resolve_limit, resolve_offset, resolve_order,
)

log = get_logger("arc.plugin.relay.discovery")

_HANDLER_DIRS = ("routes", "api")
_KNOWN_VERBS = ("get", "post", "patch", "delete")


class DiscoveryError(RuntimeError):
    """Raised at discovery for malformed declarations or route collisions."""


def _resource_routes(base: str, module_prefix: str, decl: dict, arc) -> list[tuple]:
    """Build (verb, path, handler, roles) tuples for one resource declaration.
    Dispatches to the v2 (permissions) or legacy (roles) builder."""
    if "permissions" in decl:
        return _resource_routes_v2(base, module_prefix, decl, arc)
    return _resource_routes_legacy(base, module_prefix, decl, arc)


def _resource_routes_v2(base: str, module_prefix: str, decl: dict, arc) -> list[tuple]:
    table = decl["table"]
    resource = decl.get("resource_name") or decl.get("resource") or table.lower()
    fields = decl.get("fields")
    upsert_keys = decl.get("upsert_keys")            # None → id-or-insert in arc.save
    res_limit = decl.get("limit")
    default_order = decl.get("default_order", "-updated_at")
    plugin_slug = module_prefix.split(".")[-1]
    coll = f"{base}/{plugin_slug}/{resource}"
    item = f"{coll}/{{id}}"

    permissions = decl.get("permissions") or {}
    verbs: set[str] = set()
    for role, role_verbs in permissions.items():
        for v in role_verbs:
            vl = str(v).lower()
            if vl not in _KNOWN_VERBS:
                raise DiscoveryError(
                    f"{resource}: unknown verb {v!r} for role {role!r} "
                    f"(allowed: {list(_KNOWN_VERBS)}).")
            verbs.add(vl)

    def roles_for(verb: str) -> tuple:
        return tuple(sorted(
            r for r, rv in permissions.items()
            if verb in [str(x).lower() for x in rv]))

    # filter compilation
    fdecl = decl.get("filters") or {}
    mandatory = list(fdecl.get("mandatory") or [])
    static_filters = normalize_filters(fdecl.get("static"))
    allowed_map = allowed_ops_map(fdecl.get("optional"))
    for m in mandatory:
        allowed_map.setdefault(m, set(CANON_OPS))
    query_fields = list(decl.get("query") or [])

    async def _list(ctx):
        qfilters, controls = parse_qs(ctx.query, allowed=allowed_map)
        present = {f[0] for f in qfilters}
        missing = [m for m in mandatory if m not in present]
        if missing:
            raise BadParam(f"missing mandatory filter(s): {', '.join(missing)}")
        combined = static_filters + qfilters
        order = resolve_order(controls, default=default_order)
        limit = resolve_limit(controls, resource_limit=res_limit, hard_cap=arc.list_cap)
        offset = resolve_offset(controls)
        q = controls.get("q")
        search = (query_fields, q) if (q and query_fields) else None
        return await arc.list(table, fields=fields, filters=combined or None,
                              order=order, limit=limit, offset=offset, search=search)

    async def _get(ctx):
        row = await arc.get(table, {"id": ctx.params["id"]})
        if row is None:
            raise NotFoundError(f"{resource} not found.")
        return row

    async def _save(ctx):
        return await arc.save(table, ctx.data, match_on=upsert_keys)

    async def _update(ctx):
        return await arc.update(table, {"id": ctx.params["id"]}, ctx.data)

    async def _rm(ctx):
        return await arc.rm(table, {"id": ctx.params["id"]})

    routes: list[tuple] = []
    if "get" in verbs:
        routes.append(("get", coll, _list, roles_for("get")))
        routes.append(("get", item, _get, roles_for("get")))
    if "post" in verbs:
        routes.append(("post", coll, _save, roles_for("post")))
    if "patch" in verbs:
        routes.append(("patch", item, _update, roles_for("patch")))
    if "delete" in verbs:
        routes.append(("delete", item, _rm, roles_for("delete")))
    return routes


def _resource_routes_legacy(base: str, module_prefix: str, decl: dict, arc) -> list[tuple]:
    """Original 4-route shape for declarations that predate `permissions`."""
    table = decl["table"]
    resource = decl.get("resource") or table.lower()
    fields = decl.get("fields")
    roles = tuple(decl.get("roles", ()))
    plugin_slug = module_prefix.split(".")[-1]
    coll = f"{base}/{plugin_slug}/{resource}"
    item = f"{coll}/{{id}}"

    async def _list(ctx):
        return await arc.list(table, fields=fields, filters=ctx.query or None)

    async def _get(ctx):
        row = await arc.get(table, {"id": ctx.params["id"]})
        if row is None:
            raise NotFoundError(f"{resource} not found.")
        return row

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
            generated = _resource_routes(base, module_prefix, decl, arc)
            if not generated:
                log.warning("arc.relay.resource_no_routes", plugin=slug,
                            resource=decl.get("resource_name") or decl.get("resource")
                            or decl["table"],
                            detail="no permissions declared → no routes generated")
            for verb, path, handler, roles in generated:
                method = verb.upper()
                resource_paths.add((method, path))
                getattr(registrar, verb)(path, roles=roles, table=decl["table"],
                                         source=f"{slug}:resource")(handler)
            log.info("arc.relay.resource_discovered", plugin=slug,
                     resource=decl.get("resource_name") or decl.get("resource")
                     or decl["table"], routes=len(generated))

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
    #    (method, path) duplicates a generated resource route.
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