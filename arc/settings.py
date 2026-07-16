"""
arc.settings
-------------------
Implements the single `get()` / `set()` / `delete()` surface described in the
ARC settings design: one call site for every key, secret or not. The manager
decides internally which store a key belongs in.

Layout on disk:
    .arc/arc.toml     -> [settings] table for plain config
                      -> [secrets]  declared = ["key1", "key2"]   (NAMES only, never values)
    .arc/arc.secrets  -> encrypted store holding the actual secret VALUES
    .arc/arc.mkey     -> master key used to encrypt/decrypt arc.secrets

A key is treated as secret if (a) the caller passes secret=True on `set`, or
(b) the key already appears in [secrets].declared — so callers never have to
remember whether a key is secret on every subsequent get().
"""

from __future__ import annotations

from pathlib import Path

import tomlkit
from tomlkit import TOMLDocument

from . import secrets as secret_store

REDACTED = "********"


class SettingsError(RuntimeError):
    pass


class SettingsManager:
    def __init__(self, arc_dir: Path):
        self.arc_dir = arc_dir
        self.toml_path = arc_dir / "arc.toml"
        self.secrets_path = arc_dir / "arc.secrets"
        self.mkey_path = arc_dir / "arc.mkey"

        if not self.toml_path.exists():
            raise SettingsError(
                f"{self.toml_path} not found. Run `arc init` first."
            )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _read_toml(self) -> TOMLDocument:
        return tomlkit.parse(self.toml_path.read_text())

    def _write_toml(self, doc: TOMLDocument) -> None:
        self.toml_path.write_text(tomlkit.dumps(doc))

    def _declared_secret_keys(self, doc: TOMLDocument) -> list[str]:
        return list(doc.get("secrets", {}).get("declared", []))

    def _declare_secret_key(self, doc: TOMLDocument, key: str) -> None:
        secrets_table = doc.setdefault("secrets", tomlkit.table())
        declared = secrets_table.setdefault("declared", tomlkit.array())
        if key not in declared:
            declared.append(key)

    def _undeclare_secret_key(self, doc: TOMLDocument, key: str) -> None:
        secrets_table = doc.get("secrets")
        if not secrets_table:
            return
        declared = secrets_table.get("declared")
        if declared and key in declared:
            declared.remove(key)

    def is_secret(self, key: str) -> bool:
        doc = self._read_toml()
        return key in self._declared_secret_keys(doc)

    # ------------------------------------------------------------------ #
    # Public API — mirrors arc.settings.get/set/delete at runtime
    # ------------------------------------------------------------------ #
    def get(self, key: str, reveal: bool = False) -> str | None:
        doc = self._read_toml()

        if key in self._declared_secret_keys(doc):
            values = secret_store.load(self.secrets_path, self.mkey_path)
            value = values.get(key)
            if value is None:
                return None
            return value if reveal else REDACTED

        settings_table = doc.get("settings", {})
        value = settings_table.get(key)
        return str(value) if value is not None else None

    def set(self, key: str, value: str, secret: bool = False) -> None:
        doc = self._read_toml()

        if secret:
            secret_store.save_value(self.secrets_path, self.mkey_path, key, value)
            self._declare_secret_key(doc, key)
            # If the same key previously existed as a plain setting, remove it
            # so there is exactly one source of truth per key.
            settings_table = doc.get("settings")
            if settings_table and key in settings_table:
                del settings_table[key]
            self._write_toml(doc)
            return

        if key in self._declared_secret_keys(doc):
            raise SettingsError(
                f"'{key}' is declared as a secret. Use --secret to update it, "
                f"or delete it first if you want it to become a plain setting."
            )

        settings_table = doc.setdefault("settings", tomlkit.table())
        settings_table[key] = value
        self._write_toml(doc)

    def delete(self, key: str) -> bool:
        doc = self._read_toml()

        if key in self._declared_secret_keys(doc):
            existed = secret_store.delete_value(self.secrets_path, self.mkey_path, key)
            self._undeclare_secret_key(doc, key)
            self._write_toml(doc)
            return existed

        settings_table = doc.get("settings")
        if settings_table and key in settings_table:
            del settings_table[key]
            self._write_toml(doc)
            return True
        return False

    # ------------------------------------------------------------------ #
    # NEW — needed by arc.boot() / plugin register(): declare a key's
    # secret-ness up front, with no value yet (§3.5, "typically by the
    # owning plugin"), and let boot inspect which secrets provider is
    # configured so it can emit the local-file advisory.
    # ------------------------------------------------------------------ #
    def declare(self, key: str, secret: bool = False) -> None:
        """
        Declare a key's secret-ness without setting a value yet. Idempotent.
        Mirrors the existing get/set collision rules: a key already declared
        the other way must be resolved explicitly rather than silently
        flipped.
        """
        doc = self._read_toml()
        declared = key in self._declared_secret_keys(doc)

        if secret:
            if declared:
                return
            settings_table = doc.get("settings")
            if settings_table and key in settings_table:
                raise SettingsError(
                    f"'{key}' already exists as a plain setting. Use "
                    f"set('{key}', <value>, secret=True) to migrate its value "
                    f"into the secret store, or delete it first."
                )
            self._declare_secret_key(doc, key)
            self._write_toml(doc)
        elif declared:
            raise SettingsError(
                f"'{key}' is declared as a secret; delete it first if it "
                f"should become a plain setting."
            )
        # declaring a plain key that isn't already a secret is a no-op —
        # plain keys need no declaration to be set.

    def secrets_provider(self) -> str:
        """The configured [secrets].provider — 'local_file' when unset (§3.5)."""
        doc = self._read_toml()
        secrets_table = doc.get("secrets", {})
        return str(secrets_table.get("provider", "local_file"))

    # ------------------------------------------------------------------ #
    # NEW — enumeration. Every other method here answers "what's the value
    # of THIS key", assuming the caller already knows the key exists (a
    # plugin declares its own keys at boot). Nothing previously answered
    # "what keys exist at all" — a real gap once something (admin's own
    # Settings page) needs to show the whole picture rather than one
    # already-known key. Read-only, additive, and structurally incapable
    # of leaking a secret value: it reads [settings] directly (already
    # plaintext on disk) but only ever lists secret NAMES from
    # [secrets].declared, never touching secret_store.load() at all.
    # ------------------------------------------------------------------ #
    def list_all(self) -> dict:
        doc = self._read_toml()
        # tomlkit hands back its own String/Array wrapper types (it
        # preserves formatting/whitespace for round-tripping), not plain
        # Python str — msgspec's encoder doesn't recognize them and raises
        # "Encoding objects of type String is unsupported". get() above
        # already casts a single value with str(value) for this exact
        # reason; this does the same for every value/key here.
        settings_table = doc.get("settings", {})
        return {
            "settings": {str(k): str(v) for k, v in settings_table.items()},
            "secrets": [str(k) for k in self._declared_secret_keys(doc)],
        }


# --------------------------------------------------------------------------- #
# NEW — module-level runtime API for arc.boot().
#
# §3.5 writes this as `arc.settings.get(...)` / `arc.settings.declare(...)`:
# the `settings` module itself is the runtime surface application code and
# plugins use after boot, proxying to the booted kernel's project-bound
# SettingsManager (the same class the CLI uses directly, above). Defined at
# the very end of the file so the builtin `set` is untouched by everything
# above it.
# --------------------------------------------------------------------------- #
def _bound_manager() -> SettingsManager:
    from . import _state

    kernel = _state.get_kernel()
    if kernel is None or kernel.settings is None:
        raise SettingsError(
            "arc.settings is not bound to a project — call arc.boot() (from "
            "inside an ARC project) before using arc.settings.get/set/delete/"
            "declare. Outside the runtime, use the CLI: `arc settings ...`."
        )
    return kernel.settings


def get(key: str, reveal: bool = False) -> str | None:
    return _bound_manager().get(key, reveal=reveal)


def set(key: str, value: str, secret: bool = False) -> None:  # noqa: A001 - deliberate API name
    return _bound_manager().set(key, value, secret=secret)


def delete(key: str) -> bool:
    return _bound_manager().delete(key)


def declare(key: str, secret: bool = False) -> None:
    return _bound_manager().declare(key, secret=secret)


def is_secret(key: str) -> bool:
    return _bound_manager().is_secret(key)


def list_all() -> dict:
    return _bound_manager().list_all()