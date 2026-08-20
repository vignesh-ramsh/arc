"""
arc.crypto
-----------------
General-purpose one-way hashing (hash/verify_hash) and reversible
symmetric encryption (encrypt/decrypt). Re-exported as bare arc.hash/
arc.verify_hash/arc.encrypt/arc.decrypt (arc/__init__.py) — the same "a
handful of true kernel-lifecycle-shaped primitives sit at the top level,
everything else is a namespaced module" split arc.boot/arc.shutdown
already established alongside arc.codec/arc.tz.

hash()/verify_hash() are pure, stdlib-only (hashlib/hmac) — work before
arc.boot(), same posture as arc.codec. encrypt()/decrypt() need nothing
extra either when the caller supplies its own `key`; only the no-key
default path (this project's own master key) needs a booted kernel to
resolve .arc/arc.mkey's path.

Deliberately NOT for password hashing — hash() is a fast, unsalted
digest, the wrong primitive entirely for a password (which needs a slow,
salted, purpose-built KDF instead). That's arc.authn's job, not this
module's.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import codec as _codec

# Fixed, distinct HKDF context — separates the Fernet key encrypt()/
# decrypt() actually use from arc.secrets' own (arc.secrets uses the raw
# master-key bytes AS the Fernet key directly, no HKDF at all) even
# though both ultimately derive from the same .arc/arc.mkey file. A leak
# or rotation of one key is never a leak or rotation of the other.
_HKDF_INFO = b"arc.crypto.encrypt"


class CryptoError(ValueError):
    """hash()/verify_hash() never raise this — nothing about hashing a
    value or comparing two strings can meaningfully fail. Only
    encrypt()/decrypt(): no project bound and no explicit key= given, or
    (decrypt only) a wrong key / a corrupted or tampered token."""


def hash(payload: Any) -> str:  # noqa: A001 - deliberate API name, arc.hash(...)
    """SHA-256 hex digest of `payload` — general-purpose fast one-way
    hashing (an API token, a session id, a content checksum), NOT for
    passwords (see this module's own docstring for why).

    A str/bytes payload is hashed as its raw bytes directly, exactly what
    `hashlib.sha256(raw.encode()).hexdigest()` already did everywhere
    this replaces — load-bearing for anyone with an EXISTING stored hash
    computed that way (a session's token_hash, an access key's key_hash):
    the same raw string must keep hashing to the same digest. Anything
    else (a dict, a number, a list, ...) is encoded via arc.codec first,
    so this still works uniformly on whatever you actually have."""
    data: bytes | str = payload if isinstance(payload, (str, bytes)) else _codec.encode(payload)
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def verify_hash(a: str | bytes, b: str | bytes) -> bool:
    """Constant-time comparison of two hashes/digests/tokens —
    hmac.compare_digest under the hood, given a discoverable name so
    nobody has to already know not to write `a == b` for this (a naive
    `==` short-circuits at the first mismatched byte — a real timing
    side-channel for anything secret-derived, like a token hash)."""
    if isinstance(a, str):
        a = a.encode()
    if isinstance(b, str):
        b = b.encode()
    return hmac.compare_digest(a, b)


def _derive_fernet_key(key_material: bytes) -> bytes:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        key_material
    )
    return base64.urlsafe_b64encode(derived)


def _resolve_key(key: str | bytes | None) -> bytes:
    if key is not None:
        material = key.encode() if isinstance(key, str) else key
        return _derive_fernet_key(material)

    from . import _state
    from .secrets import read_master_key

    kernel = _state.get_kernel()
    if kernel is None or kernel.settings is None:
        raise CryptoError(
            "arc.encrypt()/arc.decrypt() with no key= needs a bound project (this "
            "project's own master key, .arc/arc.mkey) — call arc.boot() first, or "
            "pass key=... explicitly to encrypt/decrypt without one."
        )
    return _derive_fernet_key(read_master_key(kernel.settings.mkey_path))


def encrypt(value: Any, *, key: str | bytes | None = None) -> str:
    """Encrypts `value` — anything arc.codec can encode: a string, a
    dict, a number, a list, whatever — and returns a URL-safe token
    string.

    `key=None` (default): encrypted with a key derived from THIS
    project's own master key (.arc/arc.mkey) — see _HKDF_INFO above for
    why that's a separate derived key, never the same key material
    arc.secrets itself uses. Requires arc.boot() to have already run.

    `key=<str | bytes>`: encrypted with a key derived from THAT value
    instead — no project binding needed at all. Any string or bytes
    works; you never need to hand-construct a "real" Fernet key
    yourself."""
    fernet = Fernet(_resolve_key(key))
    return fernet.encrypt(_codec.encode(value)).decode()


def decrypt(token: str | bytes, *, key: str | bytes | None = None) -> Any:
    """Reverses encrypt() — same key resolution rules, and must be given
    the identical `key=` (or lack of one) encrypt() was called with, or
    this raises CryptoError. Also raises CryptoError for a corrupted or
    tampered-with token — never silently returns garbage."""
    fernet = Fernet(_resolve_key(key))
    raw = token.encode() if isinstance(token, str) else token
    try:
        plaintext = fernet.decrypt(raw)
    except InvalidToken as exc:
        raise CryptoError("wrong key, or the token is corrupted/tampered with.") from exc
    return _codec.decode(plaintext)
