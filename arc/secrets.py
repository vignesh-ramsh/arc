"""
arc.secrets
------------------
Local-file secrets provider.

This is the dev / self-hosted default described in the ARC settings design:
values are encrypted at rest using a Fernet key derived from `.arc/arc.mkey`,
and are never written to arc.toml or any non-secret config file.

Cloud providers (Vault, AWS Secrets Manager, Azure Key Vault) implement the
same three functions (`load`, `save_value`, `delete_value`) against their own
backend — this module is one interchangeable implementation of that shape,
not a hardcoded dependency of the settings layer above it.
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


def _fernet_from_mkey(mkey_path: Path) -> Fernet:
    if not mkey_path.exists():
        raise SecretsError(
            f"Master key not found at {mkey_path}. Run `arc init` first."
        )
    mkey_hex = mkey_path.read_text().strip()
    try:
        raw = bytes.fromhex(mkey_hex)
    except ValueError as exc:
        raise SecretsError(f"Master key at {mkey_path} is not valid hex.") from exc
    if len(raw) != 32:
        raise SecretsError(
            f"Master key at {mkey_path} must decode to 32 bytes, got {len(raw)}."
        )
    fernet_key = base64.urlsafe_b64encode(raw)
    return Fernet(fernet_key)


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


def _write(secrets_path: Path, mkey_path: Path, data: dict[str, str]) -> None:
    fernet = _fernet_from_mkey(mkey_path)
    plaintext = json.dumps(data, sort_keys=True).encode("utf-8")
    token = fernet.encrypt(plaintext)
    secrets_path.write_bytes(token)
    secrets_path.chmod(0o600)


def save_value(secrets_path: Path, mkey_path: Path, key: str, value: str) -> None:
    """Set a single secret key, preserving all others already stored."""
    data = load(secrets_path, mkey_path)
    data[key] = value
    _write(secrets_path, mkey_path, data)


def delete_value(secrets_path: Path, mkey_path: Path, key: str) -> bool:
    """Remove a single secret key. Returns True if it existed."""
    data = load(secrets_path, mkey_path)
    if key not in data:
        return False
    del data[key]
    _write(secrets_path, mkey_path, data)
    return True
