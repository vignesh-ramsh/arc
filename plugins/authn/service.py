"""
plugins.authn.service
======================
``AuthService`` — all auth logic in one place. It reaches the database only
through the relay ``arc`` gateway (and the ``db.session`` capability for the one
hard-DELETE purge), so it imports no other plugin's internals.

Token model (hybrid, as agreed):
  • access  — stateless JWT, short TTL, verified per-request by signature only.
  • refresh — JWT whose jti is recorded as AuthSession.session_key. Revocation,
              the per-user cap and oldest-session eviction all operate on that
              registry; pure-stateless tokens cannot be evicted.

A process-wide singleton ``auth_service`` is created in ``plugins.authn`` and
bound at plugin ``setup()`` — mirroring how relay binds ``arc``. Handlers and
the CLI use the singleton; tests can construct their own and inject a fake arc.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from arc.kernel.context import UserContext, set_user
from arc.kernel.logger import get_logger
from plugins.relay import ConflictError

from plugins.authn.config import AuthConfig
from plugins.authn.errors import AuthError, ForbiddenError, LockedError
from plugins.authn.security import passwords, tokens

log = get_logger("arc.plugin.authn.service")

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_REVOKED = "revoked"
STATUS_EVICTED = "evicted"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _dumps(value: Any) -> str | None:
    """Serialise a JSON-typed field for writing through the arc gateway.

    The gateway treats JSON columns as opaque strings — it does NOT serialise
    Python objects — and asyncpg's JSONB codec requires a str, so lists/dicts
    must be dumped here. None maps to SQL NULL.
    """
    return None if value is None else json.dumps(value)


def _loads_json(value: Any) -> Any:
    """Parse a JSON field read back from the DB. asyncpg/SQLAlchemy usually
    deserialise JSONB to a Python object already; this is defensive in case a
    raw string comes back."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _as_dt(value: Any) -> dt.datetime | None:
    """Normalise a DB value (datetime or ISO string) to an aware datetime."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


class AuthService:
    def __init__(self) -> None:
        self._config: AuthConfig | None = None
        self._arc = None
        self._session_cm = None

    # ── binding (called from plugin.setup) ──────────────────────────────
    def bind(self, config: AuthConfig, arc, session_cm) -> None:
        self._config = config
        self._arc = arc
        self._session_cm = session_cm

    @property
    def config(self) -> AuthConfig:
        if self._config is None:
            raise RuntimeError("auth_service is not initialised — authn.setup() not run yet.")
        return self._config

    # ════════════════════════════════════════════════════════════════════
    # Request authentication (relay before_req hook)
    # ════════════════════════════════════════════════════════════════════
    async def authenticate_request(self, ctx) -> None:
        """Populate UserContext from a Bearer access token.

        Missing header → anonymous (route/role guards decide what to do).
        Present-but-invalid → 401. Returns None so the relay pipeline continues.
        """
        header = ctx.request.headers.get("authorization")
        if not header:
            return None

        scheme, _, raw = header.partition(" ")
        if scheme.lower() != "bearer" or not raw.strip():
            raise AuthError("Malformed Authorization header; expected 'Bearer <token>'.")

        cfg = self.config
        try:
            decoded = tokens.decode(
                raw.strip(), secret=cfg.signing_key, algorithm=cfg.algorithm,
                expect_type=tokens.ACCESS,
            )
        except tokens.TokenError as exc:
            raise AuthError(str(exc).capitalize() + ".")

        uc = UserContext(
            id=decoded.sub,
            email=decoded.email,
            roles=decoded.roles,
            permissions=(),                 # filled by Phase-5 permission resolution
            is_superuser=decoded.is_superuser,
        )
        set_user(uc)
        ctx.user = decoded.sub
        return None

    # ════════════════════════════════════════════════════════════════════
    # Login
    # ════════════════════════════════════════════════════════════════════
    async def login(self, identifier: str, password: str, *,
                    ip: str | None = None, agent: str | None = None) -> dict:
        cfg = self.config
        user = await self._load_user(identifier)

        # Uniform failure for unknown user vs bad password (no account enumeration).
        if user is None:
            raise AuthError("Invalid username or password.")

        locked_until = _as_dt(user.get("locked_until"))
        if locked_until and locked_until > _now():
            retry = int((locked_until - _now()).total_seconds())
            raise LockedError(f"Account locked. Try again in {retry} seconds.")

        if not user.get("is_active"):
            raise ForbiddenError("This account is disabled.")

        ok, needs_rehash = passwords.verify_password(user["pwd_hash"], password)
        if not ok:
            await self._register_failure(user)
            raise AuthError("Invalid username or password.")

        self._check_ip_allowed(user, ip)

        # Success — clear failure state, upgrade hash if parameters moved on.
        updates: dict[str, Any] = {"failed_logins": 0, "locked_until": None}
        if needs_rehash:
            updates["pwd_hash"] = passwords.hash_password(password)
        await self._arc.update("AuthUser", {"id": user["id"]}, updates)

        await self._enforce_session_cap(user)
        return await self._issue_for_user(user, ip=ip, agent=agent)

    # ════════════════════════════════════════════════════════════════════
    # Refresh
    # ════════════════════════════════════════════════════════════════════
    async def refresh(self, refresh_token: str, *,
                      ip: str | None = None, agent: str | None = None) -> dict:
        cfg = self.config
        try:
            decoded = tokens.decode(
                refresh_token, secret=cfg.signing_key, algorithm=cfg.algorithm,
                expect_type=tokens.REFRESH,
            )
        except tokens.TokenError as exc:
            raise AuthError(str(exc).capitalize() + ".")

        session = await self._arc.get("AuthSession", {"session_key": decoded.jti})
        if session is None or session.get("status") != STATUS_ACTIVE:
            raise AuthError("Session not found or already revoked.")

        expires_at = _as_dt(session.get("expires_at"))
        if expires_at and expires_at <= _now():
            await self._mark_session(session["id"], STATUS_EXPIRED)
            raise AuthError("Session expired; please log in again.")

        user = await self._arc.get("AuthUser", {"id": session["user_id"]})
        if user is None or not user.get("is_active"):
            raise AuthError("Account is no longer active.")

        access = self._mint_access(user)

        if cfg.session_type == "extendable":
            # Rotate the refresh token (new jti) and roll the expiry forward.
            new_jti = tokens.new_jti()
            new_exp = _now() + dt.timedelta(minutes=cfg.refresh_ttl_minutes)
            await self._arc.update("AuthSession", {"id": session["id"]}, {
                "session_key": new_jti, "last_access": _now(), "expires_at": new_exp,
                "ip": ip or session.get("ip"), "agent": agent or session.get("agent"),
            })
            refresh = tokens.encode_refresh(
                secret=cfg.signing_key, algorithm=cfg.algorithm, expires_at=new_exp,
                user_id=user["id"], username=user["username"], jti=new_jti,
            )
        else:
            # Static: fixed absolute expiry; same jti, just touch last_access.
            await self._arc.update("AuthSession", {"id": session["id"]},
                                   {"last_access": _now()})
            refresh = tokens.encode_refresh(
                secret=cfg.signing_key, algorithm=cfg.algorithm,
                expires_at=expires_at or _now(),
                user_id=user["id"], username=user["username"], jti=decoded.jti,
            )

        return self._token_envelope(access, refresh)

    # ════════════════════════════════════════════════════════════════════
    # Logout / revoke
    # ════════════════════════════════════════════════════════════════════
    async def logout(self, refresh_token: str) -> bool:
        """Revoke the session behind a refresh token. Idempotent — an invalid or
        already-gone token returns False without error."""
        cfg = self.config
        try:
            decoded = tokens.decode(
                refresh_token, secret=cfg.signing_key, algorithm=cfg.algorithm,
                expect_type=tokens.REFRESH,
            )
        except tokens.TokenError:
            return False

        session = await self._arc.get("AuthSession", {"session_key": decoded.jti})
        if session is None:
            return False
        await self._revoke_session(session["id"])
        return True

    async def revoke_all(self, user_id: str) -> int:
        """Revoke every active session for a user (e.g. after a password reset)."""
        rows = await self._arc.list(
            "AuthSession", fields=["id"],
            filters=[("user_id", "eq", user_id), ("status", "eq", STATUS_ACTIVE)],
            limit=self.config.default_max_sessions * 10,
        )
        for row in rows:
            await self._revoke_session(row["id"])
        return len(rows)

    # ════════════════════════════════════════════════════════════════════
    # Admin helpers (used by CLI + guarded admin routes)
    # ════════════════════════════════════════════════════════════════════
    async def create_user(self, username: str, email: str, password: str, *,
                          is_superuser: bool = False,
                          roles: list[str] | None = None) -> dict:
        username = (username or "").strip()
        email = (email or "").strip().lower()
        if not username or not email:
            raise AuthError("username and email are required.")
        if await self._load_user(username) or await self._load_user(email):
            raise ConflictError(f"A user with that username or email already exists.")

        self._require_strength(password, username=username, email=email)

        row = {
            "username": username,
            "email": email,
            "pwd_hash": passwords.hash_password(password),
            "is_superuser": bool(is_superuser),
            "is_active": True,
            "failed_logins": 0,
            "roles": _dumps(list(roles or [])),
            "allowed_ips": _dumps([]),
            "last_pwd_reset_at": _now(),
        }
        await self._arc.save("AuthUser", row)
        log.info("arc.authn.user_created", username=username, superuser=is_superuser)
        return {"username": username, "email": email, "is_superuser": is_superuser}

    async def set_active(self, username: str, active: bool) -> bool:
        """Enable/disable a user. Idempotent — returns True if state changed."""
        user = await self._load_user(username)
        if user is None:
            raise AuthError(f"No such user: {username}")
        if bool(user.get("is_active")) == active:
            return False
        await self._arc.update("AuthUser", {"id": user["id"]}, {"is_active": active})
        if not active:                      # disabling kills live sessions
            await self.revoke_all(user["id"])
        log.info("arc.authn.user_active_changed", username=username, active=active)
        return True

    async def set_password(self, username: str, password: str) -> None:
        user = await self._load_user(username)
        if user is None:
            raise AuthError(f"No such user: {username}")
        self._require_strength(password, username=user["username"], email=user["email"])
        await self._arc.update("AuthUser", {"id": user["id"]}, {
            "pwd_hash": passwords.hash_password(password),
            "last_pwd_reset_at": _now(),
        })
        await self.revoke_all(user["id"])   # force re-login everywhere
        log.info("arc.authn.password_reset", username=username)

    async def create_session_for(self, username: str, *,
                                 ip: str | None = None, agent: str | None = None) -> dict:
        """Mint a token pair out-of-band (service accounts / admin). Deliberate —
        normally sessions are born from a real login."""
        user = await self._load_user(username)
        if user is None:
            raise AuthError(f"No such user: {username}")
        if not user.get("is_active"):
            raise ForbiddenError("This account is disabled.")
        await self._enforce_session_cap(user)
        return await self._issue_for_user(user, ip=ip, agent=agent)

    async def purge_sessions(self, *, before: dt.datetime | None = None,
                             dry_run: bool = False) -> int:
        """Hard-DELETE session rows past their retention cutoff.

        Sessions are high-churn and double as an activity log; routing them
        through _trash (soft delete) would bloat it, so purge issues a real
        DELETE via db.session — the same contract psqldb's cleanup uses.
        """
        from sqlalchemy import text
        cutoff = before or (_now() - dt.timedelta(days=self.config.session_retention_days))

        if dry_run:
            rows = await self._arc.query(
                'SELECT count(*) AS n FROM "AuthSession" WHERE expires_at < :c',
                {"c": cutoff},
            )
            return int(rows[0]["n"]) if rows else 0

        # get_session() commits on clean exit (read_only defaults False), so the
        # DELETE is persisted when the context manager exits without error.
        async with self._session_cm() as s:
            res = await s.execute(
                text('DELETE FROM "AuthSession" WHERE expires_at < :c'), {"c": cutoff})
            count = res.rowcount if res.rowcount is not None else 0
        log.info("arc.authn.sessions_purged", count=count, cutoff=cutoff.isoformat())
        return count

    # ════════════════════════════════════════════════════════════════════
    # Internals
    # ════════════════════════════════════════════════════════════════════
    async def _load_user(self, identifier: str) -> dict | None:
        rows = await self._arc.query(
            'SELECT * FROM "AuthUser" '
            'WHERE (username = :ident OR email = :ident) AND "_state" != 99 '
            'LIMIT 1',
            {"ident": (identifier or "").strip()},
        )
        if not rows:
            return None
        row = dict(rows[0])
        row["roles"] = _loads_json(row.get("roles")) or []
        row["allowed_ips"] = _loads_json(row.get("allowed_ips")) or []
        return row

    async def _register_failure(self, user: dict) -> None:
        cfg = self.config
        n = int(user.get("failed_logins") or 0) + 1
        updates: dict[str, Any] = {"failed_logins": n}
        if n >= cfg.lockout_threshold:
            updates["locked_until"] = _now() + dt.timedelta(seconds=cfg.lockout_seconds)
            log.warning("arc.authn.account_locked",
                        username=user.get("username"), failures=n)
        await self._arc.update("AuthUser", {"id": user["id"]}, updates)

    def _check_ip_allowed(self, user: dict, ip: str | None) -> None:
        """An allowlist RESTRICTS login to those addresses — it never replaces
        the password. Empty/absent list means no restriction."""
        allowed = user.get("allowed_ips") or []
        if not allowed:
            return
        if ip is None or ip not in set(allowed):
            log.warning("arc.authn.ip_rejected", username=user.get("username"), ip=ip)
            raise ForbiddenError("Login is not permitted from this network address.")

    def _max_sessions(self, user: dict) -> int:
        override = user.get("max_sessions")
        return int(override) if override else self.config.default_max_sessions

    async def _enforce_session_cap(self, user: dict) -> None:
        cap = self._max_sessions(user)
        active = await self._arc.count(
            "AuthSession", {"user_id": user["id"], "status": STATUS_ACTIVE})
        # Evict oldest-first until a new session fits under the cap.
        while active >= cap:
            oldest = await self._arc.list(
                "AuthSession", fields=["id"],
                filters=[("user_id", "eq", user["id"]),
                         ("status", "eq", STATUS_ACTIVE)],
                order="created_at", limit=1,
            )
            if not oldest:
                break
            await self._mark_session(oldest[0]["id"], STATUS_EVICTED)
            await self._arc.rm("AuthSession", {"id": oldest[0]["id"]})
            active -= 1

    async def _issue_for_user(self, user: dict, *, ip, agent) -> dict:
        cfg = self.config
        jti = tokens.new_jti()
        expires_at = _now() + dt.timedelta(minutes=cfg.refresh_ttl_minutes)
        await self._arc.save("AuthSession", {
            "session_key": jti,
            "user_id": user["id"],
            "username": user["username"],
            "last_access": _now(),
            "expires_at": expires_at,
            "ip": ip,
            "agent": (agent or "")[:255] or None,
            "status": STATUS_ACTIVE,
        })
        access = self._mint_access(user)
        refresh = tokens.encode_refresh(
            secret=cfg.signing_key, algorithm=cfg.algorithm, expires_at=expires_at,
            user_id=user["id"], username=user["username"], jti=jti,
        )
        log.info("arc.authn.login_ok", username=user["username"])
        return self._token_envelope(access, refresh)

    def _mint_access(self, user: dict) -> str:
        cfg = self.config
        return tokens.encode_access(
            secret=cfg.signing_key, algorithm=cfg.algorithm,
            ttl_minutes=cfg.access_ttl_minutes,
            user_id=user["id"], username=user["username"], email=user["email"],
            roles=list(user.get("roles") or []), is_superuser=bool(user.get("is_superuser")),
        )

    def _token_envelope(self, access: str, refresh: str) -> dict:
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": self.config.access_ttl_minutes * 60,
        }

    async def _mark_session(self, session_id, status: str) -> None:
        await self._arc.update("AuthSession", {"id": session_id}, {"status": status})

    async def _revoke_session(self, session_id) -> None:
        await self._mark_session(session_id, STATUS_REVOKED)
        await self._arc.rm("AuthSession", {"id": session_id})

    def _require_strength(self, password: str, *, username: str, email: str) -> None:
        score, reason = passwords.score_password(
            password or "", username=username, email=email)
        if score < self.config.min_password_score:
            raise AuthError(
                f"Password too weak (score {score}/4, need "
                f"{self.config.min_password_score}). {reason}")