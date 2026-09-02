"""
arc.settings
-------------------
Implements the single `get()` / `set()` / `delete()` surface described in the
ARC settings design: one call site for every key, secret or not. The manager
decides internally which store a key belongs in.

Layout on disk:
    .arc/arc.toml      -> [project]/[logging]/[secrets].provider only —
                           structural/bootstrap config, still plain text
                           and git-tracked, unrelated to per-key values.
    .arc/arc.store.db  -> SQLite: every setting AND secret value (see
                           arc.store's own docstring for the table shapes),
                           plus a reveal-only secret access log. Never
                           git-tracked — same posture .arc/arc.secrets had.
    .arc/arc.mkey      -> master key used to encrypt/decrypt secret values
                           in arc.store.db.

A key is treated as secret if (a) the caller passes secret=True on `set`, or
(b) the key is already marked secret in arc.store.db — so callers never have
to remember whether a key is secret on every subsequent get().

A project created before this (a plain-text .arc/arc.toml [settings] table
+ a Fernet-blob .arc/arc.secrets) is migrated automatically, once, the first
time SettingsManager opens it — see _migrate_legacy_store_if_needed below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument

from . import secrets as legacy_secrets_store
from . import store

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
            f"{value!r} is not a valid boolean (expected one of: true/false, 1/0, yes/no, on/off)"
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
        self.db_path = arc_dir / "arc.store.db"
        self.mkey_path = arc_dir / "arc.mkey"

        if not self.toml_path.exists():
            raise SettingsError(f"{self.toml_path} not found. Run `arc init` first.")

        # (mtime_ns, size) -> parsed document. arc.toml now only ever holds
        # [project]/[logging]/[secrets].provider — small and rarely written —
        # but the same re-parse-is-measurable reasoning still applies to
        # secrets_provider() being called from a hot path, so the cache
        # stays. Invalidates on any write, including one from another
        # process.
        self._toml_cache: tuple[tuple[int, int], TOMLDocument] | None = None
        # key -> SettingSpec, populated by declare(type=...) calls during
        # each plugin's register(kernel). Process-local only — never
        # persisted, since a key's type/default/doc is a code fact the
        # owning plugin restates on every boot, not user config.
        self._declared_specs: dict[str, SettingSpec] = {}

        # key -> already-coerced value, for PLAIN (non-secret) keys only.
        # get() is on genuinely hot paths — gateway reads
        # gateway_maintenance_mode before routing EVERY request — and used
        # to run a real SQLite SELECT there every time. (The comment at that
        # call site claimed the read was a cached stat(), which was true of
        # the pre-SQLite arc.toml store and stayed behind after the move.)
        #
        # Never caches a secret: a masked read is cheap anyway, and a
        # revealed one must keep hitting reveal_secret() so it keeps
        # logging. Invalidated by _cache_epoch() below.
        self._value_cache: dict[str, Any] = {}
        self._cache_data_version: int | None = None
        self._cache_write_epoch: int = -1

        self._migrate_legacy_store_if_needed()
        self._conn = store.open_db(self.db_path)

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

    def _drop_value_cache_if_stale(self) -> None:
        """Clears _value_cache when anything may have changed a setting's
        value since we last looked. Two independent signals, because
        neither one alone catches every writer:

        `PRAGMA data_version` catches ANOTHER PROCESS. SQLite bumps it
        when a different connection commits, and deliberately not for
        this connection's own writes. It's a header read, not a query
        against `setting`, so it's cheap enough for a per-request caller.
        Deliberately not a stat() on the db file the way _read_toml's own
        cache works: the store runs in WAL mode (store.open_db), so a
        committed write can land in arc.store.db-wal and leave the main
        file's mtime untouched.

        `store.write_epoch()` catches EVERY IN-PROCESS WRITER, including
        a different SettingsManager instance — which matters because
        store.open_db() hands every manager in a process the same cached
        connection, so a sibling manager's write bumps no data_version
        here at all while still being completely invisible to this
        object's own cache. That's not hypothetical: it's how
        `arc set-maintenance` is modelled in tests, and how any two
        managers in one process interact."""
        version = self._conn.execute("PRAGMA data_version").fetchone()[0]
        epoch = store.write_epoch()
        if version != self._cache_data_version or epoch != self._cache_write_epoch:
            self._value_cache.clear()
            self._cache_data_version = version
            self._cache_write_epoch = epoch

    def _write_toml(self, doc: TOMLDocument) -> None:
        # tmp-then-replace, not a direct write_text — two processes writing
        # arc.toml around the same moment could otherwise interleave,
        # leaving a truncated/corrupt TOML file on disk that every
        # subsequent read fails to parse. Path.replace() is a single atomic
        # rename on the same filesystem.
        tmp_path = self.toml_path.with_name(self.toml_path.name + f".tmp.{os.getpid()}")
        tmp_path.write_text(tomlkit.dumps(doc))
        tmp_path.replace(self.toml_path)
        self._toml_cache = None

    def _migrate_legacy_store_if_needed(self) -> None:
        """One-time, automatic migration from the pre-SQLite layout
        (.arc/arc.toml's [settings]/[secrets].declared + .arc/arc.secrets,
        a single Fernet-encrypted blob) into .arc/arc.store.db. Runs at
        most once — a no-op the instant arc.store.db exists, which is true
        for every project created after this. Non-destructive: the legacy
        .arc/arc.secrets is renamed to *.pre-sqlite.bak, never deleted, so
        a migration nobody's verified yet can always be inspected or rolled
        back by hand rather than trusting it blindly."""
        if self.db_path.exists():
            return

        doc = self._read_toml()
        settings_table = doc.get("settings")
        secrets_table = doc.get("secrets", {})
        declared_secret_keys = list(secrets_table.get("declared", []))
        if not settings_table and not declared_secret_keys:
            return  # fresh project — store.open_db() below just creates empty tables

        conn = store.open_db(self.db_path)
        if settings_table:
            for key, raw in settings_table.items():
                store.set_plain(conn, str(key), str(raw), updated_by="migration:legacy-toml")

        legacy_secrets_path = self.arc_dir / "arc.secrets"
        if declared_secret_keys and legacy_secrets_path.exists() and legacy_secrets_path.stat().st_size:
            legacy_values = legacy_secrets_store.load(legacy_secrets_path, self.mkey_path)
            for key in declared_secret_keys:
                value = legacy_values.get(key)
                if value is not None:
                    store.set_secret(conn, self.mkey_path, key, value, updated_by="migration:legacy-toml")
                else:
                    store.declare_secret_key(conn, key)

        # Strip [settings]/[secrets].declared from arc.toml now that values
        # live in arc.store.db — [project]/[logging]/[secrets].provider are
        # untouched, they were never this migration's concern.
        if "settings" in doc:
            del doc["settings"]
        if "secrets" in doc:
            provider = doc["secrets"].get("provider", "local_file")
            new_secrets_table = tomlkit.table()
            new_secrets_table["provider"] = provider
            doc["secrets"] = new_secrets_table
        self._write_toml(doc)

        if legacy_secrets_path.exists():
            legacy_secrets_path.replace(self.arc_dir / "arc.secrets.pre-sqlite.bak")

    def is_secret(self, key: str) -> bool:
        return store.is_secret(self._conn, key)

    # ------------------------------------------------------------------ #
    # Public API — mirrors arc.settings.get/set/delete at runtime
    # ------------------------------------------------------------------ #
    def get(self, key: str, reveal: bool = False, accessed_by: str | None = None) -> Any:
        """Returns the coerced type when `key` was declare()'d with one
        (§1 P0) — plain `str | None` otherwise, unchanged from before. A
        secret key's REDACTED placeholder is returned as-is, never coerced
        (coercion only ever touches a real value, with `reveal=True`).

        `accessed_by` is only meaningful (and only ever persisted) when
        `reveal=True` on a secret key — that's the one path that writes a
        secret_access_log row (arc.store.reveal_secret). A masked read
        never logs anything, by design: logging every redacted read would
        spam the log every time a superuser's Settings page renders the
        key list without revealing a single value."""
        spec = self._declared_specs.get(key)

        # Plain-key fast path — see _value_cache's own comment. Skipped
        # entirely for reveal=True so a real secret read always reaches
        # reveal_secret() and always logs.
        if not reveal:
            self._drop_value_cache_if_stale()
            if key in self._value_cache:
                return self._value_cache[key]

        value, is_secret_key = store.get_setting(self._conn, key)

        if is_secret_key:
            if not reveal:
                has_value = store.secret_has_value(self._conn, key)
                return REDACTED if has_value else (spec.default if spec is not None else None)
            real = store.reveal_secret(self._conn, self.mkey_path, key, accessed_by)
            if real is None:
                return spec.default if spec is not None else None
            return self._coerce_or_raise(key, real, spec)

        resolved = (
            (spec.default if spec is not None else None)
            if value is None
            else self._coerce_or_raise(key, value, spec)
        )
        # Cached only on the plain-key path (a secret already returned
        # above, either masked or through the logging reveal path).
        self._value_cache[key] = resolved
        return resolved

    def secret_values_for_redaction(self) -> list[str]:
        """Every stored secret's real value, for scrubbing them OUT of text
        about to be shown to someone — never for showing one TO someone.
        Does not write a secret_access_log row; see
        arc.store.all_secret_values' own docstring for why that's correct
        here and wrong everywhere else."""
        return store.all_secret_values(self._conn, self.mkey_path)

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

    def set(self, key: str, value: str, secret: bool = False, updated_by: str | None = None) -> None:
        # Cleared explicitly, not left to _drop_value_cache_if_stale():
        # PRAGMA data_version deliberately does NOT move for this
        # connection's own commits, so a write made through THIS manager
        # would otherwise keep serving the pre-write value out of cache.
        self._value_cache.pop(key, None)
        if secret:
            store.set_secret(self._conn, self.mkey_path, key, value, updated_by)
            return

        if store.is_secret(self._conn, key):
            raise SettingsError(
                f"'{key}' is declared as a secret. Use --secret to update it, "
                f"or delete it first if you want it to become a plain setting."
            )

        store.set_plain(self._conn, key, value, updated_by)

    def delete(self, key: str) -> bool:
        self._value_cache.pop(key, None)  # same reasoning as set() above
        return store.delete_key(self._conn, key)

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

        # A declare() changes what get() should RETURN for this key (its
        # type coercion and its default), so anything cached from a read
        # that happened before the owning plugin's register() ran — a raw
        # uncoerced string, or a None where there's now a real default — is
        # wrong the instant this lands.
        self._value_cache.pop(key, None)

        declared = store.is_secret(self._conn, key)

        if secret:
            if not declared:
                value, existing_is_secret = store.get_setting(self._conn, key)
                if not existing_is_secret and value is not None:
                    raise SettingsError(
                        f"'{key}' already exists as a plain setting. Use "
                        f"set('{key}', <value>, secret=True) to migrate its value "
                        f"into the secret store, or delete it first."
                    )
                store.declare_secret_key(self._conn, key)
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
    # of leaking a secret value: arc.store.list_all() never touches the
    # `secret` table at all, only `setting`'s bookkeeping columns.
    # ------------------------------------------------------------------ #
    def list_all(self) -> dict:
        """One entry per key that either has a value in arc.store.db or was
        declare()'d this boot (so a never-set key with a default still
        shows up — §1 P0: "types/defaults/docs for free"). A secret key's
        `value` is always None here — only reveal_secret()-style callers
        using get(key, reveal=True) ever see a real secret value, and only
        that path ever writes a secret_access_log row."""
        rows = store.list_all(self._conn)

        # NOT `set(...)` (function calls) here — this module defines its
        # own module-level `set()` (mirrors SettingsManager.set, deliberate
        # API-name shadow), which shadows the builtin for every function in
        # this file at call time. Set-display syntax sidesteps the name
        # entirely rather than relying on which `set` happens to resolve.
        keys = {*rows.keys(), *self._declared_specs.keys()}
        out: dict[str, dict] = {}
        for key in sorted(keys):
            spec = self._declared_specs.get(key)
            row = rows.get(key)
            is_secret_key = bool(row and row["is_secret"])
            value = row["value"] if row else None
            out[str(key)] = {
                "value": value,
                "kind": "secret" if is_secret_key else "setting",
                "type": spec.type.__name__ if spec and spec.type else None,
                "default": spec.default if spec else None,
                "doc": spec.doc if spec else "",
            }
        return out

    def access_log(self, key: str | None = None, limit: int = 100) -> list[dict]:
        """Recent secret_access_log rows — one per reveal=True read, most
        recent first. §"Explain me about Secrets Management" follow-up:
        the one piece of vault-style auditability a flat-file store never
        had at all."""
        return store.access_log(self._conn, key=key, limit=limit)


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


def get(key: str, reveal: bool = False, accessed_by: str | None = None) -> Any:
    return _bound_manager().get(key, reveal=reveal, accessed_by=accessed_by)


def set(  # noqa: A001 - deliberate API name
    key: str, value: str, secret: bool = False, updated_by: str | None = None
) -> None:
    return _bound_manager().set(key, value, secret=secret, updated_by=updated_by)


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


def access_log(key: str | None = None, limit: int = 100) -> list[dict]:
    return _bound_manager().access_log(key=key, limit=limit)
