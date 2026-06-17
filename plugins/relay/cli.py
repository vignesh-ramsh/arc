"""
plugins.relay.cli
=================
``arc relay routes`` / ``arc relay hooks`` — inspect what discovery registered.
Routes show the declarative metadata (roles / rt_limit / stream) so a config
audit is one command.
"""

from __future__ import annotations

import typer


def build_cli() -> typer.Typer:
    app = typer.Typer(name="relay", help="Inspect relay routes and hooks.",
                      no_args_is_help=True)

    @app.command()
    def routes() -> None:
        """List every route registered through relay."""
        from plugins.relay import relay

        specs = sorted(relay.routes, key=lambda s: (s.path, s.methods))
        if not specs:
            typer.echo("No relay routes registered.")
            return
        for s in specs:
            methods = ",".join(s.methods) + ("/stream" if s.stream else "")
            roles = ",".join(s.roles) if s.roles else "(authenticated)"
            if s.is_guest:
                roles = "Guest(public)"
            rt = f"{s.rate_limit.count}/min/user" if s.rate_limit else "default"
            typer.echo(f"  {methods:<14} {s.path:<34} roles={roles:<22} rt={rt}  ({s.source})")
        typer.echo(f"\n{len(specs)} route(s).")

    @app.command()
    def hooks() -> None:
        """List document hooks grouped by table/event, plus global hooks."""
        from plugins.relay import relay

        summary = relay.hook_summary()
        if summary:
            for table, event, count in summary:
                typer.echo(f"  {table:<24} {event:<16} {count} hook(s)")
        else:
            typer.echo("No document hooks registered.")

        for phase in ("on_commit", "on_rollback"):
            n = len(relay.tx_hooks(phase))
            if n:
                typer.echo(f"  {'<transaction>':<24} {phase:<16} {n} hook(s)")
        for phase in ("before_req", "after_req"):
            n = len(relay.req_hooks(phase))
            if n:
                typer.echo(f"  {'<request>':<24} {phase:<16} {n} hook(s)")

    return app


__all__ = ["build_cli"]