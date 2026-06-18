"""
plugins.relay.cli
=================
``arc relay routes`` / ``arc relay hooks`` — inspect what discovery registered.

Both commands accept filters:
    -p, --plugin   only items owned by this plugin (e.g. hrms, sales)
    -t, --table    only items operating on this table (e.g. Employee, Order)

Examples:
    arc relay routes                 # everything
    arc relay routes -p sales        # sales routes only
    arc relay routes -t Order        # routes that touch the Order table
    arc relay hooks  -p hrms -t Leave
"""

from __future__ import annotations

import typer


def _plugin_of(source_or_module: str) -> str:
    """Resolve the owning plugin slug from a route source or a hook __module__.

      "sales:resource"                  → "sales"
      "plugins.sales.routes.orders"     → "sales"
      "sales.routes.orders"             → "sales"
    """
    if not source_or_module:
        return ""
    if ":" in source_or_module:                     # "sales:resource"
        return source_or_module.split(":", 1)[0]
    parts = source_or_module.split(".")
    if parts and parts[0] == "plugins" and len(parts) > 1:
        return parts[1]                             # plugins.<slug>.…
    return parts[0]                                 # <slug>.…


def build_cli() -> typer.Typer:
    app = typer.Typer(name="relay", help="Inspect relay routes and hooks.",
                      no_args_is_help=True)

    plugin_opt = typer.Option(None, "-p", "--plugin", help="Filter by plugin slug.")
    table_opt = typer.Option(None, "-t", "--table", help="Filter by table name.")

    @app.command()
    def routes(plugin: str = plugin_opt, table: str = table_opt) -> None:
        """List routes registered through relay (optionally filtered)."""
        from plugins.relay import relay

        specs = sorted(relay.routes, key=lambda s: (s.path, s.methods))
        if plugin:
            specs = [s for s in specs if _plugin_of(s.source) == plugin]
        if table:
            specs = [s for s in specs if s.table == table]

        scope = _scope_label(plugin, table)
        if not specs:
            typer.echo(f"No relay routes registered{scope}.")
            return

        for s in specs:
            methods = ",".join(s.methods) + ("/stream" if s.stream else "")
            roles = ",".join(s.roles) if s.roles else "(authenticated)"
            if s.is_guest:
                roles = "Guest(public)"
            rt = f"{s.rate_limit.count}/min/user" if s.rate_limit else "default"
            tbl = s.table or "-"
            typer.echo(f"  {methods:<14} {s.path:<34} table={tbl:<14} "
                       f"roles={roles:<22} rt={rt}  ({s.source})")
        typer.echo(f"\n{len(specs)} route(s){scope}.")

    @app.command()
    def hooks(plugin: str = plugin_opt, table: str = table_opt) -> None:
        """List document hooks by table/event (optionally filtered), plus globals."""
        from plugins.relay import relay

        items = relay.hook_items()   # [(table, event, [fns]), ...]
        if table:
            items = [(t, e, fns) for (t, e, fns) in items if t == table]

        rows: list[tuple[str, str, int]] = []
        for t, e, fns in items:
            if plugin:
                fns = [fn for fn in fns if _plugin_of(getattr(fn, "__module__", "")) == plugin]
            if fns:
                rows.append((t, e, len(fns)))

        scope = _scope_label(plugin, table)
        if rows:
            for t, e, count in rows:
                typer.echo(f"  {t:<24} {e:<16} {count} hook(s)")
        else:
            typer.echo(f"No document hooks registered{scope}.")

        # Global hooks have no table; show them only when not table-filtered.
        if not table:
            for phase in ("on_commit", "on_rollback"):
                fns = relay.tx_hooks(phase)
                if plugin:
                    fns = [fn for fn in fns
                           if _plugin_of(getattr(fn, "__module__", "")) == plugin]
                if fns:
                    typer.echo(f"  {'<transaction>':<24} {phase:<16} {len(fns)} hook(s)")
            for phase in ("before_req", "after_req"):
                fns = relay.req_hooks(phase)
                if plugin:
                    fns = [fn for fn in fns
                           if _plugin_of(getattr(fn, "__module__", "")) == plugin]
                if fns:
                    typer.echo(f"  {'<request>':<24} {phase:<16} {len(fns)} hook(s)")

    return app


def _scope_label(plugin: str | None, table: str | None) -> str:
    bits = []
    if plugin:
        bits.append(f"plugin={plugin}")
    if table:
        bits.append(f"table={table}")
    return f" ({', '.join(bits)})" if bits else ""


__all__ = ["build_cli"]