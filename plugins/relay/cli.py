"""
plugins.relay.cli
====================
``arc relay routes`` / ``arc relay hooks`` — inspect what's registered. Useful
for confirming a plugin's decorators were picked up and seeing hook coverage
per table (data-integrity audit).
"""

from __future__ import annotations

import typer


def build_cli() -> typer.Typer:
    app = typer.Typer(name="relay", help="Inspect relay routes and hooks.", no_args_is_help=True)

    @app.command()
    def routes() -> None:
        """List every route registered through relay."""
        from plugins.relay import relay

        specs = sorted(relay.routes, key=lambda s: (s.path, s.methods))
        if not specs:
            typer.echo("No relay routes registered.")
            return
        for s in specs:
            methods = ",".join(s.methods)
            typer.echo(f"  {methods:<20} {s.path:<32} ({s.source})")
        typer.echo(f"\n{len(specs)} route(s).")

    @app.command()
    def hooks() -> None:
        """List document-event hooks grouped by table/event."""
        from plugins.relay import relay

        summary = relay.hook_summary()
        if not summary:
            typer.echo("No relay hooks registered.")
            return
        for table, event, count in summary:
            typer.echo(f"  {table:<24} {event:<16} {count} hook(s)")
        typer.echo(f"\n{len(summary)} binding(s).")

    return app