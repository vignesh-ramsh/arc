"""
plugins.authn.config
=====================
Typed view over the ``[plugins.authn]`` table in ``arc.toml`` plus the JWT
signing secret, which is read from the environment — NEVER from arc.toml
(same rule as DATABASE_URL and the backup key).

Every knob the proposal called "configurable in TOML" lives here:

    session_type            "static" | "extendable"   (#3)
    access_ttl_minutes      access-token lifetime      (#4)
    refresh_ttl_minutes     refresh-token / session lifetime (#4)
    default_max_sessions    per-user cap, overridable by AuthUser.max_sessions (#4/#5)
    session_retention_days  purge cutoff for AuthSession (#6)
    lockout_threshold       consecutive failures before lock (#9)
    lockout_seconds         lock duration (#9)
    min_password_score      0..4 strength floor (#10)
    trust_forwarded         honour X-Forwarded-For for the client IP (#8)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from arc.kernel.exceptions import ArcError

VALID_SESSION_TYPES = ("static", "extendable")
VALID_ALGORITHMS = ("HS256", "HS384", "HS512")


class AuthConfigError(ArcError):
    """Raised when authn config is invalid or the signing secret is missing."""


@dataclass(frozen=True)
class AuthConfig:
    signing_key: str
    algorithm: str = "HS256"
    session_type: str = "extendable"
    access_ttl_minutes: int = 15
    refresh_ttl_minutes: int = 60 * 24 * 7          # 7 days
    default_max_sessions: int = 5
    session_retention_days: int = 60
    lockout_threshold: int = 3
    lockout_seconds: int = 300
    min_password_score: int = 3
    trust_forwarded: bool = False

    @classmethod
    def from_runtime(cls, plugin_config: dict | None, env: dict | None = None) -> "AuthConfig":
        cfg = dict(plugin_config or {})
        env = env if env is not None else os.environ

        key_env = str(cfg.get("signing_key_env", "ARC_AUTHN_SECRET"))
        secret = env.get(key_env, "")
        if not secret or not secret.strip():
            raise AuthConfigError(
                f"authn signing secret is not set. Export {key_env}=<long-random-string> "
                f"(set 'signing_key_env' in [plugins.authn] to change the variable name). "
                f"Never put the secret in arc.toml.",
                code="arc.authn.no_secret",
            )

        algorithm = str(cfg.get("algorithm", "HS256")).upper()
        if algorithm not in VALID_ALGORITHMS:
            raise AuthConfigError(
                f"Unsupported algorithm {algorithm!r}. Supported: {VALID_ALGORITHMS}.",
                code="arc.authn.bad_algorithm",
            )

        session_type = str(cfg.get("session_type", "extendable")).lower()
        if session_type not in VALID_SESSION_TYPES:
            raise AuthConfigError(
                f"session_type must be one of {VALID_SESSION_TYPES}, got {session_type!r}.",
                code="arc.authn.bad_session_type",
            )

        def _int(name: str, default: int, *, minimum: int = 0) -> int:
            try:
                val = int(cfg.get(name, default))
            except (TypeError, ValueError):
                raise AuthConfigError(f"[plugins.authn] {name} must be an integer.",
                                      code="arc.authn.bad_int")
            if val < minimum:
                raise AuthConfigError(f"[plugins.authn] {name} must be >= {minimum}.",
                                      code="arc.authn.bad_int")
            return val

        return cls(
            signing_key=secret,
            algorithm=algorithm,
            session_type=session_type,
            access_ttl_minutes=_int("access_ttl_minutes", 15, minimum=1),
            refresh_ttl_minutes=_int("refresh_ttl_minutes", 60 * 24 * 7, minimum=1),
            default_max_sessions=_int("default_max_sessions", 5, minimum=1),
            session_retention_days=_int("session_retention_days", 60, minimum=1),
            lockout_threshold=_int("lockout_threshold", 3, minimum=1),
            lockout_seconds=_int("lockout_seconds", 300, minimum=0),
            min_password_score=_int("min_password_score", 3, minimum=0),
            trust_forwarded=bool(cfg.get("trust_forwarded", False)),
        )
