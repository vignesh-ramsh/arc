"""
arc.plugins.api.cli
=================
The ``arc api`` command group — inspect the routes the api plugin would mount.
"""

from __future__ import annotations

import typer

from arc.kernel.registry import Points


def build_cli() -> typer.Typer:
    api_app = typer.Typer(name="api", help="Inspect the REST surface.")

    @api_app.command("routes")
    def routes_cmd() -> None:
        """List every HTTP route Arc would serve."""
        from arc.kernel.orchestrator import Arc

        arc = Arc()
        arc.build()
        for route in arc.extensions.get(Points.HTTP_ROUTES):
            methods = ",".join(sorted(getattr(route, "methods", []) or []))
            typer.echo(f"  {methods:<18} {getattr(route, 'path', route)}")

    return api_app
