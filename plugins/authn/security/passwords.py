"""
plugins.authn.security.passwords
================================
Password hashing (argon2id) and a pragmatic strength scorer.

Hashing uses argon2-cffi's ``PasswordHasher`` — argon2id is the OWASP-recommended
default. ``verify`` also reports whether the stored hash should be re-hashed with
current parameters (transparent upgrade on next successful login).

Strength scoring (#10) is deliberately dependency-free and NIST 800-63B-flavoured:
length dominates, character variety helps, and a small blocklist rejects the
obvious. It returns 0..4 so a single ``min_password_score`` floor in config
expresses "complexity required". Swap in zxcvbn later behind ``score_password``
without touching callers.

Requires: argon2-cffi  (add to pyproject: argon2-cffi>=23.1)
"""

from __future__ import annotations

import re

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerifyMismatchError
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "authn requires argon2-cffi. Install it: pip install argon2-cffi"
    ) from exc

# One shared hasher — argon2id with library defaults (sane memory/time costs).
_hasher = PasswordHasher()

# Tiny blocklist — illustrative. In production load a breached-password set
# (e.g. the Pwned Passwords k-anonymity API or a local bloom filter).
_COMMON = frozenset({
    "password", "passw0rd", "123456", "12345678", "qwerty", "abc123",
    "letmein", "admin", "welcome", "iloveyou", "monkey", "dragon",
    "111111", "000000", "changeme", "root", "toor", "secret",
})


def hash_password(plain: str) -> str:
    """Return the argon2id encoded hash (includes algorithm, params and salt)."""
    return _hasher.hash(plain)


def verify_password(stored_hash: str, plain: str) -> tuple[bool, bool]:
    """Return ``(ok, needs_rehash)``.

    ``ok`` is True if *plain* matches *stored_hash*. ``needs_rehash`` is True
    when the stored hash used weaker parameters than the current policy — the
    caller should re-hash and persist on a successful login.
    """
    try:
        _hasher.verify(stored_hash, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False, False
    try:
        return True, _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True, False


def score_password(plain: str, *, username: str = "", email: str = "") -> tuple[int, str]:
    """Score a password 0 (very weak) .. 4 (strong) with a reason string.

    Heuristics:
      * length is the primary driver (>=12 strong, >=8 acceptable)
      * variety (lower/upper/digit/symbol classes) adds incrementally
      * exact-match to a common password, or containing the username/email
        local-part, caps the score at 0
    """
    pw = plain or ""
    lowered = pw.lower()

    if lowered in _COMMON:
        return 0, "This is a commonly used password."

    local = (email.split("@", 1)[0] if email else "").lower()
    if username and username.lower() in lowered:
        return 0, "Password must not contain your username."
    if local and len(local) >= 3 and local in lowered:
        return 0, "Password must not contain your email name."

    if len(pw) < 8:
        return 1, "Password must be at least 8 characters."

    classes = sum(bool(re.search(p, pw)) for p in (
        r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]",
    ))

    score = 0
    if len(pw) >= 8:
        score += 1
    if len(pw) >= 12:
        score += 1
    if len(pw) >= 16:
        score += 1
    score += max(0, classes - 1)        # 1 class = +0, 4 classes = +3
    score = max(1, min(4, score))

    reason = {
        1: "Weak — add length and character variety.",
        2: "Fair — longer is stronger.",
        3: "Good.",
        4: "Strong.",
    }[score]
    return score, reason
