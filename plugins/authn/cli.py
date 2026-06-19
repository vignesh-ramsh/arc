"""
plugins.authn.cli
=================
``arc authn`` admin commands, contributed to Points.CLI_COMMANDS.

    arc authn create-user alice -e alice@acme.io [--superuser] [--role Admin]
    arc authn enable-user alice            # idempotent
    arc authn disable-user alice           # idempotent (also revokes live sessions)
    arc authn set-password alice           # prompts; revokes all sessions
    arc authn create-session -u alice      # mint a token pair out-of-band
    arc authn purge-sessions [--days N] [--dry-run]
    arc authn list-sessions -u alice

Commands are synchronous Typer entry points that drive the async AuthService via
asyncio.run — the same shape psqldb's CLI uses.
"""

from __future__ import annotations

import asyncio

import typer

from arc.kernel.exceptions import ArcError
from arc.kernel.orchestrator import Arc
from plugins.relay import ConflictError

from plugins.authn import auth_service
from plugins.authn.errors import AuthError, ForbiddenError, LockedError


def _run(coro):
    """Run an AuthService coroutine inside a started kernel lifecycle.

    CLI commands run outside the ASGI lifespan, so psqldb's engine/session
    factory — created in its async startup() — is not yet initialised, and the
    arc gateway would fail with 'session factory not initialised'. We boot the
    shared, already-built Arc's lifecycle (which creates the engine and fully
    arms arc), run the coroutine, then tear it down. Only the public orchestrator
    API is touched — no psqldb internals are imported.
    """
    async def _bootstrap():
        app = Arc.shared()                   # built once when the CLI mounted commands
        assert app.lifecycle is not None
        own = not app.lifecycle.started
        if own:
            await app.lifecycle.startup()    # psqldb.startup() initialises the factory
        try:
            return await coro
        finally:
            if own:
                await app.lifecycle.shutdown()

    try:
        return asyncio.run(_bootstrap())
    except (AuthError, ForbiddenError, LockedError, ConflictError, ArcError) as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def build_cli() -> typer.Typer:
    app = typer.Typer(name="authn", help="Manage auth users and sessions.",
                      no_args_is_help=True)

    @app.command("create-user")
    def create_user(
        username: str = typer.Argument(..., help="Unique username."),
        email: str = typer.Option(..., "-e", "--email", help="Unique email."),
        password: str = typer.Option(None, "--password",
                                     help="Omit to be prompted securely."),
        superuser: bool = typer.Option(False, "--superuser",
                                       help="Bypass all role checks."),
        role: list[str] = typer.Option(None, "-r", "--role",
                                       help="Grant a role (repeatable)."),
    ) -> None:
        """Create a user (password is hashed with argon2id)."""
        if not password:
            password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        res = _run(auth_service.create_user(
            username, email, password, is_superuser=superuser, roles=role or []))
        typer.secho(f"created user {res['username']} <{res['email']}>"
                    + (" [superuser]" if res["is_superuser"] else ""),
                    fg=typer.colors.GREEN)

    @app.command("enable-user")
    def enable_user(username: str = typer.Argument(...)) -> None:
        """Activate a user. Idempotent."""
        changed = _run(auth_service.set_active(username, True))
        typer.echo(f"{username} enabled." if changed else f"{username} already enabled.")

    @app.command("disable-user")
    def disable_user(username: str = typer.Argument(...)) -> None:
        """Deactivate a user and revoke live sessions. Idempotent."""
        changed = _run(auth_service.set_active(username, False))
        typer.echo(f"{username} disabled." if changed else f"{username} already disabled.")

    @app.command("set-password")
    def set_password(
        username: str = typer.Argument(...),
        password: str = typer.Option(None, "--password",
                                     help="Omit to be prompted securely."),
    ) -> None:
        """Reset a user's password (revokes all their sessions)."""
        if not password:
            password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
        _run(auth_service.set_password(username, password))
        typer.secho(f"password updated for {username}; sessions revoked.",
                    fg=typer.colors.GREEN)

    @app.command("create-session")
    def create_session(
        username: str = typer.Option(..., "-u", "--user", help="Session owner."),
    ) -> None:
        """Mint a token pair for a user without a login (service accounts)."""
        env = _run(auth_service.create_session_for(username, agent="cli"))
        typer.echo("access_token:  " + env["access_token"])
        typer.echo("refresh_token: " + env["refresh_token"])
        typer.echo(f"expires_in:    {env['expires_in']}s (access)")

    @app.command("purge-sessions")
    def purge_sessions(
        days: int = typer.Option(None, "--days",
                                 help="Override retention cutoff in days."),
        dry_run: bool = typer.Option(False, "--dry-run",
                                     help="Count what would be deleted."),
    ) -> None:
        """Hard-delete expired session rows past the retention cutoff."""
        import datetime as dt
        before = None
        if days is not None:
            before = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        n = _run(auth_service.purge_sessions(before=before, dry_run=dry_run))
        verb = "would purge" if dry_run else "purged"
        typer.echo(f"{verb} {n} session(s).")

    @app.command("list-sessions")
    def list_sessions(
        username: str = typer.Option(..., "-u", "--user", help="Session owner."),
    ) -> None:
        """List active sessions for a user."""
        async def _coro():
            user = await auth_service._load_user(username)
            if user is None:
                raise AuthError(f"No such user: {username}")
            return await auth_service._arc.list(
                "AuthSession",
                fields=["session_key", "ip", "agent", "last_access", "expires_at", "status"],
                filters=[("user_id", "eq", user["id"]), ("status", "eq", "active")],
                order="-created_at", limit=100,
            )
        rows = _run(_coro())
        if not rows:
            typer.echo(f"No active sessions for {username}.")
            return
        for r in rows:
            typer.echo(f"  {r.get('session_key','')[:12]}…  ip={r.get('ip') or '-':<15} "
                       f"expires={r.get('expires_at')}  {r.get('agent') or ''}")
        typer.echo(f"\n{len(rows)} active session(s).")

    return app