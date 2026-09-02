"""
arc.secrets
------------------
Master-key primitives shared by every encrypted-at-rest store in ARC.

`read_master_key()`/`_fernet_from_mkey()` turn `.arc/arc.mkey`'s raw 32
bytes into the Fernet key used two independent ways: `arc.store` (settings
+ secrets, SQLite-backed) uses it directly as the Fernet key for the
`secret` table's ciphertext; `arc.crypto` HKDF-derives a *different* subkey
from the same root for business-data encrypt()/decrypt() — see
arc.crypto's own docstring for why that separation matters.

`load()` below is the read side of the OLD flat-file secrets store
(`.arc/arc.secrets`, one Fernet-encrypted JSON blob for every secret) —
kept only so `arc.settings`'s one-time migration into `.arc/arc.store.db`
(SQLite) can still decrypt a pre-existing store. Nothing writes that old
format anymore; new/migrated projects go straight to `arc.store`.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretsError(RuntimeError):
    """Raised for any secrets-store failure (missing key, corrupt store, etc.)."""


# (mtime_ns, size) -> decrypted dict, keyed by store path. Fernet-decrypting
# the whole store on every secret read is measurable on request paths (mail
# reads an SMTP credential per delivery). Invalidates automatically on any
# write, including from another process, via the stat key. load() hands out
# copies so a caller mutating its dict can never poison the cache.
_load_cache: dict[Path, tuple[tuple[int, int], dict[str, str]]] = {}

# (mtime_ns, size) -> Fernet, keyed by master-key path. Same stat-key idiom
# (and same reasoning) as _load_cache above, applied to the OTHER repeated
# cost on a secret read: _fernet_from_mkey used to re-read .arc/arc.mkey off
# disk and reconstruct a Fernet on every single call, so anything reading N
# secrets in a row paid N reads of one unchanged file. Rotation still takes
# effect immediately — writing a new master key changes its mtime/size,
# which misses the cache by construction.
_fernet_cache: dict[Path, tuple[tuple[int, int], Fernet]] = {}


def read_master_key(mkey_path: Path) -> bytes:
    """The project's one root secret — raw 32 bytes, hex-decoded from
    .arc/arc.mkey. Public (unlike this module's other internals): shared
    by _fernet_from_mkey below (this module's own settings-secrets
    encryption, the raw bytes used AS the Fernet key directly) and
    arc.crypto's business-data encrypt()/decrypt() (a separate,
    HKDF-derived subkey from this SAME root — see arc.crypto's own
    docstring for why that matters: a leak or rotation of one must never
    implicate the other, even though both ultimately trace back to this
    one file)."""
    if not mkey_path.exists():
        raise SecretsError(f"Master key not found at {mkey_path}. Run `arc init` first.")
    mkey_hex = mkey_path.read_text().strip()
    try:
        raw = bytes.fromhex(mkey_hex)
    except ValueError as exc:
        raise SecretsError(f"Master key at {mkey_path} is not valid hex.") from exc
    if len(raw) != 32:
        raise SecretsError(f"Master key at {mkey_path} must decode to 32 bytes, got {len(raw)}.")
    return raw


def _fernet_from_mkey(mkey_path: Path) -> Fernet:
    """Memoized on (mtime_ns, size) — see _fernet_cache's own comment. A
    missing/unreadable key file falls through to the uncached path so
    read_master_key() below still raises its own clear SecretsError rather
    than this stat() raising a bare OSError first."""
    try:
        stat = mkey_path.stat()
    except OSError:
        return Fernet(base64.urlsafe_b64encode(read_master_key(mkey_path)))
    cache_key = (stat.st_mtime_ns, stat.st_size)
    cached = _fernet_cache.get(mkey_path)
    if cached is not None and cached[0] == cache_key:
        return cached[1]
    fernet = Fernet(base64.urlsafe_b64encode(read_master_key(mkey_path)))
    _fernet_cache[mkey_path] = (cache_key, fernet)
    return fernet


def load(secrets_path: Path, mkey_path: Path) -> dict[str, str]:
    """Decrypt and return the full secrets dict. Empty dict if store is empty/new."""
    if not secrets_path.exists() or secrets_path.stat().st_size == 0:
        return {}

    stat = secrets_path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _load_cache.get(secrets_path)
    if cached is not None and cached[0] == key:
        return dict(cached[1])

    fernet = _fernet_from_mkey(mkey_path)
    token = secrets_path.read_bytes()
    try:
        plaintext = fernet.decrypt(token)
    except InvalidToken as exc:
        raise SecretsError(
            f"Could not decrypt {secrets_path} — wrong master key, or the store is corrupt."
        ) from exc
    data = json.loads(plaintext.decode("utf-8"))
    _load_cache[secrets_path] = (key, dict(data))
    return data
