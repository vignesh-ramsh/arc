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

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument

from . import secrets as secret_store

REDACTED = "********"

# Types a declare()'d setting may coerce to. Kept small and closed rather
# than "any callable" — a typed setting is meant to be a plain scalar read
# from a TOML string or a secret-store string, not an arbitrary parser.
_COERCIBLE_TYPES = (int, float, bool, str)


class SettingsError(RuntimeError):
    pass


def _coerce(key: str, value: str, type_: type) -> Any:
    """Parse `value` (always a plain string on disk/in the secret store) as
    `type_`. Raises ValueError on a bad value — callers turn that into a
    SettingsError naming the offending key."""
    if type_ is bool:
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(
            f"{value!r} is not a valid boolean (expected one of: "
            f"true/false, 1/0, yes/no, on/off)"
        )
    if type_ is int:
        return int(value)
    if type_ is float:
        return float(value)
    return value  # type_ is str — already a string, nothing to do


@dataclass(frozen=True)
class SettingSpec:
    """What a plugin declared about one settings key at register() time —
    everything `arc settings list` / admin's Settings page needs to show
    types/defaults/docs without the key ever having been set (§1 P0)."""
    key: str
    type: type | None
    default: Any
    doc: str


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
        # (mtime_ns, size) -> parsed document. get() runs on request paths
        # (authn reads TTL/lockout settings on every login, mail reads a
        # credential per delivery) — re-parsing arc.toml from disk with
        # tomlkit on every call is a real, measurable cost there. The key
        # invalidates on any write, including one from another process.
        self._toml_cache: tuple[tuple[int, int], TOMLDocument] | None = None
        # key -> SettingSpec, populated by declare(type=...) calls during
        # each plugin's register(kernel). Process-local only — never
        # persisted to arc.toml, since a key's type/default/doc is a code
        # fact the owning plugin restates on every boot, not user config.
        self._declared_specs: dict[str, SettingSpec] = {}

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _read_toml(self) -> TOMLDocument:
        stat = self.toml_path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        if self._toml_cache is not None and self._toml_cache[0] == key:
            return self._toml_cache[1]
        doc = tomlkit.parse(self.toml_path.read_text())
        self._toml_cache = (key, doc)
        return doc

    def _write_toml(self, doc: TOMLDocument) -> None:
        self.toml_path.write_text(tomlkit.dumps(doc))
        # Mutation flows (set/delete/declare) mutate the cached document
        # in place before writing — drop the cache so the next read
        # re-parses from disk rather than trusting a possibly-dirty object.
        self._toml_cache = None

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
    def get(self, key: str, reveal: bool = False) -> Any:
        """Returns the coerced type when `key` was declare()'d with one
        (§1 P0) — plain `str | None` otherwise, unchanged from before. A
        secret key's REDACTED placeholder is returned as-is, never coerced
        (coercion only ever touches a real value, with `reveal=True`)."""
        doc = self._read_toml()
        spec = self._declared_specs.get(key)

        if key in self._declared_secret_keys(doc):
            values = secret_store.load(self.secrets_path, self.mkey_path)
            value = values.get(key)
            if value is None:
                return spec.default if spec is not None else None
            if not reveal:
                return REDACTED
            return self._coerce_or_raise(key, value, spec)

        settings_table = doc.get("settings", {})
        raw = settings_table.get(key)
        if raw is None:
            return spec.default if spec is not None else None
        return self._coerce_or_raise(key, str(raw), spec)

    def _coerce_or_raise(self, key: str, value: str, spec: "SettingSpec | None") -> Any:
        if spec is None or spec.type is None:
            return value
        try:
            return _coerce(key, value, spec.type)
        except ValueError as exc:
            raise SettingsError(
                f"setting '{key}' is set to {value!r}, which is not a valid "
                f"{spec.type.__name__}{f' ({spec.doc})' if spec.doc else ''} — "
                f"fix it with `arc settings set {key} <value>`, or clear it "
                f"with `arc settings delete {key}` to fall back to the "
                f"default ({spec.default!r})."
            ) from exc

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
    def declare(
        self,
        key: str,
        secret: bool = False,
        *,
        type: type | None = None,  # noqa: A002 - deliberate API name, mirrors set()'s own builtin shadow
        default: Any = None,
        doc: str = "",
    ) -> None:
        """
        Declare a key's secret-ness (as before) and, optionally, its type/
        default/doc (§1 P0) — called once per key from the owning plugin's
        register(kernel), every boot. Idempotent: re-declaring the same key
        with the same type is a no-op; a *different* type from a previous
        declare() (two call sites disagreeing about one key) is a hard
        error, same posture as the existing secret/plain collision below.

        `type` must be one of int/float/bool/str. When set, `get(key)`
        returns the coerced value instead of a raw string, and the current
        persisted value (if any) is checked against it once at boot end
        (arc.runtime.boot -> validate_declared()) so a bad value fails at
        startup, not at whatever call happens to read it first.
        """
        if type is not None and type not in _COERCIBLE_TYPES:
            raise SettingsError(
                f"declare('{key}', type={type!r}): only int/float/bool/str "
                f"are supported settings types."
            )

        toml_doc = self._read_toml()
        declared = key in self._declared_secret_keys(toml_doc)

        if secret:
            if not declared:
                settings_table = toml_doc.get("settings")
                if settings_table and key in settings_table:
                    raise SettingsError(
                        f"'{key}' already exists as a plain setting. Use "
                        f"set('{key}', <value>, secret=True) to migrate its value "
                        f"into the secret store, or delete it first."
                    )
                self._declare_secret_key(toml_doc, key)
                self._write_toml(toml_doc)
        elif declared:
            raise SettingsError(
                f"'{key}' is declared as a secret; delete it first if it "
                f"should become a plain setting."
            )
        # declaring a plain key that isn't already a secret is a no-op on
        # the secret-ness axis — plain keys need no declaration to be set.

        if type is not None:
            existing = self._declared_specs.get(key)
            if existing is not None and existing.type is not None and existing.type is not type:
                raise SettingsError(
                    f"'{key}' was already declared with type "
                    f"{existing.type.__name__}; this call declares "
                    f"{type.__name__} — two declare() call sites disagree "
                    f"about this key's type."
                )
        self._declared_specs[key] = SettingSpec(key=key, type=type, default=default, doc=doc)

    def validate_declared(self) -> None:
        """Eagerly resolve every type-declared key's current value once —
        called at the end of arc.boot(), after every plugin's register()
        has run (so every declare() has happened). A value that fails to
        parse raises here, collecting every bad key into one message,
        rather than surfacing one at a time the first moment something
        happens to call get() for it (§1 P0: "boot-time failure beats
        first-use failure")."""
        problems: list[str] = []
        for key, spec in self._declared_specs.items():
            if spec.type is None:
                continue
            try:
                self.get(key, reveal=True)
            except SettingsError as exc:
                problems.append(str(exc))
        if problems:
            raise SettingsError(
                "invalid setting value(s) found at boot:\n"
                + "\n".join(f"  - {p}" for p in problems)
            )

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
        """One entry per key that either has a value on disk or was
        declare()'d this boot (so a never-set key with a default still
        shows up — §1 P0: "types/defaults/docs for free"). Never touches
        secret_store.load(): a secret key's `value` is always None here,
        exactly like the old two-list shape's name-only listing — only
        reveal_secret()-style callers using get(key, reveal=True) ever see
        a real secret value."""
        doc = self._read_toml()
        # tomlkit hands back its own String/Array wrapper types (it
        # preserves formatting/whitespace for round-tripping), not plain
        # Python str — msgspec's encoder doesn't recognize them and raises
        # "Encoding objects of type String is unsupported". str(...) below
        # casts every value read from tomlkit for this exact reason.
        settings_table = doc.get("settings", {})
        declared_secrets = self._declared_secret_keys(doc)

        # NOT `set(...)` (function calls) here — this module defines its
        # own module-level `set()` (mirrors SettingsManager.set, deliberate
        # API-name shadow), which shadows the builtin for every function in
        # this file at call time. Set-display syntax sidesteps the name
        # entirely rather than relying on which `set` happens to resolve.
        keys = {*settings_table.keys(), *declared_secrets, *self._declared_specs.keys()}
        out: dict[str, dict] = {}
        for key in sorted(keys):
            spec = self._declared_specs.get(key)
            is_secret_key = key in declared_secrets
            raw = None if is_secret_key else settings_table.get(key)
            out[str(key)] = {
                "value": None if raw is None else str(raw),
                "kind": "secret" if is_secret_key else "setting",
                "type": spec.type.__name__ if spec and spec.type else None,
                "default": spec.default if spec else None,
                "doc": spec.doc if spec else "",
            }
        return out


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


def get(key: str, reveal: bool = False) -> Any:
    return _bound_manager().get(key, reveal=reveal)


def set(key: str, value: str, secret: bool = False) -> None:  # noqa: A001 - deliberate API name
    return _bound_manager().set(key, value, secret=secret)


def delete(key: str) -> bool:
    return _bound_manager().delete(key)


def declare(
    key: str,
    secret: bool = False,
    *,
    type: type | None = None,  # noqa: A002 - deliberate API name, see SettingsManager.declare
    default: Any = None,
    doc: str = "",
) -> None:
    return _bound_manager().declare(key, secret=secret, type=type, default=default, doc=doc)


def is_secret(key: str) -> bool:
    return _bound_manager().is_secret(key)


def list_all() -> dict:
    return _bound_manager().list_all()