"""
plugins.authn.security.tokens
=============================
Stateless JWT minting and verification. Pure functions — the signing secret,
algorithm and TTLs are passed in by ``AuthService`` (which reads them from
config), so this module holds no state and is trivially testable.

Two token types share one secret but are never interchangeable (``type`` claim
is checked on decode):

  access   short-lived, verified on EVERY request by signature + exp only.
           Carries identity + roles so the hot path needs ZERO database hits.
  refresh  longer-lived; its ``jti`` is the AuthSession.session_key. Presented
           only to /auth/refresh, which checks the session registry — that is
           where revocation and eviction bite.

Claims (access): sub, username, email, roles, su, type, iat, exp, jti
Claims (refresh): sub, username, type, iat, exp, jti

Requires: PyJWT  (add to pyproject: PyJWT>=2.8)
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

try:
    import jwt
    from jwt import ExpiredSignatureError, InvalidTokenError
except ImportError as exc:  # pragma: no cover
    raise ImportError("authn requires PyJWT. Install it: pip install PyJWT") from exc

ACCESS = "access"
REFRESH = "refresh"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_jti() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class DecodedToken:
    sub: str
    type: str
    jti: str
    username: str
    email: str
    roles: tuple[str, ...]
    is_superuser: bool
    expires_at: dt.datetime


class TokenError(Exception):
    """Raised on any decode failure (expired, bad signature, wrong type)."""


def encode_access(*, secret: str, algorithm: str, ttl_minutes: int,
                  user_id: str, username: str, email: str,
                  roles: list[str], is_superuser: bool) -> str:
    now = _now()
    payload = {
        "sub": str(user_id),
        "username": username,
        "email": email,
        "roles": list(roles or []),
        "su": bool(is_superuser),
        "type": ACCESS,
        "iat": now,
        "exp": now + dt.timedelta(minutes=ttl_minutes),
        "jti": new_jti(),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def encode_refresh(*, secret: str, algorithm: str, expires_at: dt.datetime,
                   user_id: str, username: str, jti: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "type": REFRESH,
        "iat": _now(),
        "exp": expires_at,
        "jti": jti,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode(token: str, *, secret: str, algorithm: str, expect_type: str) -> DecodedToken:
    try:
        claims = jwt.decode(token, secret, algorithms=[algorithm])
    except ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except InvalidTokenError as exc:
        raise TokenError("invalid token") from exc

    if claims.get("type") != expect_type:
        raise TokenError(f"expected a {expect_type} token")

    return DecodedToken(
        sub=str(claims.get("sub", "")),
        type=str(claims.get("type", "")),
        jti=str(claims.get("jti", "")),
        username=str(claims.get("username", "")),
        email=str(claims.get("email", "")),
        roles=tuple(claims.get("roles", []) or ()),
        is_superuser=bool(claims.get("su", False)),
        expires_at=dt.datetime.fromtimestamp(claims["exp"], tz=dt.timezone.utc)
        if "exp" in claims else _now(),
    )