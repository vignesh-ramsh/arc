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
                           plus an explicit secret access (audit) log. Never
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

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit import TOMLDocument

from . import secrets as legacy_secrets_store
from . import store

_logger = logging.getLogger("arc.settings")

REDACTED = "********"

#: Where secrets live, chosen by `[secrets].provider` in .arc/arc.toml.
#:   local_file  (default) encrypted in .arc/arc.store.db with .arc/arc.mkey; `arc settings set
#:               --secret` writes them.
#:   env         read from the environment, one variable per secret (`env_secret_name`). ARC stores
#:               nothing and cannot write: the platform injects the values (systemd
#:               EnvironmentFile, a container secret, a CI variable) - the 12-factor shape.
#: Anything else is refused. It used to be ignored, silently leaving the secrets in the local
#: file when the operator believed they were somewhere else.
SECRETS_PROVIDERS = ("local_file", "env")
ENV_SECRET_PREFIX = "ARC_SECRET_"


def env_secret_name(key: str) -> str:
    """The environment variable the `env` provider reads a secret from: `pgdb_dsn` ->
    `ARC_SECRET_PGDB_DSN`."""
    return ENV_SECRET_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", key).upper()

def cli_actor() -> str:
    """Who to record as `updated_by` for a setting written from a terminal: `cli:<os user>`. There is
    no signed-in identity at a shell, so the operating-system user stands in. The admin console
    records the signed-in user's e-mail instead."""
    return f"cli:{os.environ.get('USER') or 'unknown'}"


# Types a declare()'d setting may coerce to. Kept small and closed rather
# than "any callable" — a typed setting is meant to be a plain scalar read
# from a TOML string or a secret-store string, not an arbitrary parser.
_COERCIBLE_TYPES = (int, float, bool, str)
_TYPES_BY_NAME = {t.__name__: t for t in _COERCIBLE_TYPES}


class SettingsError(RuntimeError):
    pass


class OperatorOnlyError(SettingsError):
    """A key declared `operator_only` was written by code that did not say it is the operator.

    Such a key decides what ARC executes or which files it can reach (the command `arc restart`
    runs, the folder uploads are served from). Whoever can write it can already do anything the
    ARC process can, so it is changed on the server - `arc settings set` - and refused when asked
    through the admin console, even from a Superuser: a web login is not shell access."""


def read_secrets_provider(toml_path: Path) -> str:
    """The configured `[secrets].provider` ('local_file' when unset), validated. Reads arc.toml
    only - opens no store - so `arc doctor` can use it. Raises SettingsError for a provider ARC
    does not have."""
    doc = tomlkit.parse(toml_path.read_text())
    return _validated_provider(str(doc.get("secrets", {}).get("provider", "local_file")), toml_path)


def _validated_provider(name: str, toml_path: Path) -> str:
    if name not in SECRETS_PROVIDERS:
        raise SettingsError(
            f"the secrets provider is {name!r} (the `provider` key of the `secrets` table in {toml_path}), "
            f"which ARC does not have "
            f"(available: {', '.join(SECRETS_PROVIDERS)}). An unknown value used to be ignored, "
            f"keeping the secrets in the local file; it is refused now so that a secret you meant "
            f"to keep somewhere else is not quietly kept there."
        )
    return name


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


def _default_matches_type(default: Any, type_: type) -> bool:
    """Whether a declare(default=...) literal is already a `type_`. get()
    returns the default as-is (it is never run through _coerce), so a
    mismatched one would only surface when a caller used it. bool is a
    subclass of int, so it is excluded from int/float explicitly; an int
    default is fine for a float key."""
    if type_ is bool:
        return isinstance(default, bool)
    if isinstance(default, bool):
        return False
    if type_ is float:
        return isinstance(default, (int, float))
    return isinstance(default, type_)


@dataclass(frozen=True)
class SettingSpec:
    """What a plugin declared about one settings key at register() time —
    everything `arc settings list` / admin's Settings page needs to show
    types/defaults/docs without the key ever having been set (§1 P0)."""

    key: str
    type: type | None
    default: Any
    doc: str
    operator_only: bool = False


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
        # revealed value is never held in process memory beyond the call.
        # Invalidated by _cache_epoch() below.
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
    def get(self, key: str, reveal: bool = False) -> Any:
        """Returns the coerced type when `key` was declare()'d with one
        (§1 P0) — plain `str | None` otherwise, unchanged from before. A
        secret key's REDACTED placeholder is returned as-is, never coerced
        (coercion only ever touches a real value, with `reveal=True`).

        Reading never writes: neither a masked read nor `reveal=True`
        leaves an audit row. `reveal=True` is also how the system reads its
        own secrets on hot paths (authn's CSRF key on every cookie-authenticated
        POST), so an automatic audit write here would put a SQLite commit on
        every request. Code that shows a secret to a person records that
        explicitly with record_secret_access()."""
        spec = self._declared_specs.get(key)

        # Plain-key fast path — see _value_cache's own comment. Skipped
        # for reveal=True: a secret is read fresh from the store each time.
        if not reveal:
            self._drop_value_cache_if_stale()
            if key in self._value_cache:
                return self._value_cache[key]

        value, is_secret_key, stored_type = store.get_setting_typed(self._conn, key)

        if is_secret_key:
            if self.secrets_provider() == "env":
                return self._get_env_secret(key, reveal, spec, stored_type)
            if not reveal:
                has_value = store.secret_has_value(self._conn, key)
                return REDACTED if has_value else (spec.default if spec is not None else None)
            real = store.reveal_secret(self._conn, self.mkey_path, key)
            if real is None:
                return spec.default if spec is not None else None
            return self._coerce_or_raise(key, real, spec, stored_type)

        resolved = (
            (spec.default if spec is not None else None)
            if value is None
            else self._coerce_or_raise(key, value, spec, stored_type)
        )
        # Cached only on the plain-key path (a secret already returned
        # above, either masked or through the logging reveal path).
        self._value_cache[key] = resolved
        return resolved

    def _get_env_secret(
        self, key: str, reveal: bool, spec: "SettingSpec | None", stored_type: str | None = None
    ) -> Any:
        """A secret under the `env` provider: its environment variable, an empty one counting as
        unset. The same shapes `get` gives for the local file: REDACTED for a masked read of a
        secret that has a value, the declared default when it has none."""
        raw = os.environ.get(env_secret_name(key)) or None
        default = spec.default if spec is not None else None
        if not reveal:
            return REDACTED if raw is not None else default
        return default if raw is None else self._coerce_or_raise(key, raw, spec, stored_type)

    def secret_values_for_redaction(self) -> list[str]:
        """Every secret's real value, for scrubbing them OUT of text about to be shown to
        someone — never for showing one TO someone. Meant for redaction only; a caller that
        shows a secret to a person uses get(..., reveal=True) plus record_secret_access().
        The values stored locally, and every `ARC_SECRET_*` environment variable: a process
        under the `env` provider must scrub those too, and one that switched to it from the
        local file may still hold old values worth scrubbing."""
        values = store.all_secret_values(self._conn, self.mkey_path)
        values += [v for k, v in os.environ.items() if k.startswith(ENV_SECRET_PREFIX) and v]
        return values

    def _coerce_or_raise(
        self, key: str, value: str, spec: "SettingSpec | None", stored_type: str | None = None
    ) -> Any:
        """`value` as the key's type: the one the code declared, else the one a key added by hand was given
        (`stored_type`), else as it is."""
        type_ = spec.type if spec is not None and spec.type is not None else _TYPES_BY_NAME.get(stored_type or "")
        if type_ is None:
            return value
        try:
            return _coerce(key, value, type_)
        except ValueError as exc:
            doc = f" ({spec.doc})" if spec is not None and spec.doc else ""
            default = spec.default if spec is not None else None
            raise SettingsError(
                f"setting '{key}' is set to {value!r}, which is not a valid "
                f"{type_.__name__}{doc} — "
                f"fix it with `arc settings set {key} <value>`, or clear it "
                f"with `arc settings delete {key}` to fall back to the "
                f"default ({default!r})."
            ) from exc

    def set(
        self,
        key: str,
        value: str,
        secret: bool = False,
        updated_by: str | None = None,
        type: str | None = None,  # deliberate API name, mirrors declare()
        description: str | None = None,
        operator: bool = False,
    ) -> None:
        """Store `value` for `key`.

        `operator=True` says the caller is the person running the server (the `arc settings`
        command, `arc deploy`), which is the only way to write a key declared `operator_only`;
        anything reachable from a web request leaves it False.

        `type` (one of str/int/float/bool) and `description` are what a key added by hand is created with: they are
        accepted only when the key is new (nothing in the store and nothing declared), because a key's type is
        fixed when it is created and its description is changed through set_description(). A key that has a type -
        declared by the code or given here - only accepts a value that is one. Everything is checked before
        anything is written, so a refused call leaves nothing behind."""
        spec = self._declared_specs.get(key)
        self._require_operator(key, spec, operator)
        if type is not None or description is not None:
            self._check_new_key(key, type, spec)
        new_description = None if description is None else self._clean_description(key, description)
        if type is not None or (spec is None or spec.type is None):
            # the type the value must be: the one being given, else the one stored; a declared type is checked
            # when the value is read, as it always was
            stored_type = type if type is not None else store.get_setting_typed(self._conn, key)[2]
            if stored_type is not None:
                self._check_value_is(key, value, stored_type)

        # Cleared explicitly, not left to _drop_value_cache_if_stale():
        # PRAGMA data_version deliberately does NOT move for this
        # connection's own commits, so a write made through THIS manager
        # would otherwise keep serving the pre-write value out of cache.
        self._value_cache.pop(key, None)
        if secret:
            if self.secrets_provider() == "env":
                raise SettingsError(
                    f"cannot store the secret '{key}': the secrets provider is 'env', which only "
                    f"reads secrets from the environment. Set {env_secret_name(key)} in the "
                    f"environment of every ARC process instead (or set the `provider` key of the "
                    f"`secrets` table in .arc/arc.toml back to 'local_file')."
                )
            store.set_secret(
                self._conn, self.mkey_path, key, value, updated_by, type_=type, description=new_description
            )
            return

        if store.is_secret(self._conn, key):
            raise SettingsError(
                f"'{key}' is declared as a secret. Use --secret to update it, "
                f"or delete it first if you want it to become a plain setting."
            )

        store.set_plain(self._conn, key, value, updated_by, type_=type, description=new_description)

    def _check_new_key(self, key: str, type_: str | None, spec: SettingSpec | None) -> None:
        if type_ is not None and type_ not in _TYPES_BY_NAME:
            raise SettingsError(f"'{type_}' is not a type a setting can have (use one of: {', '.join(_TYPES_BY_NAME)}).")
        if spec is not None or store.key_exists(self._conn, key):
            raise SettingsError(
                f"'{key}' already exists. A key's type is fixed when it is created, and its description is changed "
                f"on the admin Settings page (or with set_description()), so neither can be given when setting a "
                f"value for it."
            )

    @staticmethod
    def _check_value_is(key: str, value: str, type_name: str) -> None:
        try:
            _coerce(key, value, _TYPES_BY_NAME[type_name])
        except ValueError as exc:
            detail = str(exc) if type_name == "bool" else f"{value!r} is not a valid {type_name}"
            raise SettingsError(f"'{key}' is a {type_name} setting: {detail}.") from exc

    @staticmethod
    def _require_operator(key: str, spec: SettingSpec | None, operator: bool) -> None:
        if spec is not None and spec.operator_only and not operator:
            raise OperatorOnlyError(
                f"'{key}' can only be changed on the server, with `arc settings set {key} <value>` - "
                f"it decides what ARC runs or which files it can reach, so it is not editable from the "
                f"admin console."
            )

    def is_operator_only(self, key: str) -> bool:
        """Whether `key` was declared `operator_only` (see OperatorOnlyError)."""
        spec = self._declared_specs.get(key)
        return spec is not None and spec.operator_only

    def delete(self, key: str, operator: bool = False) -> bool:
        self._require_operator(key, self._declared_specs.get(key), operator)
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
        operator_only: bool = False,
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
        if type is not None and default is not None and not _default_matches_type(default, type):
            raise SettingsError(
                f"declare('{key}', type={type.__name__}, default={default!r}): "
                f"the default is a {default.__class__.__name__}, not a "
                f"{type.__name__}."
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
        self._declared_specs[key] = SettingSpec(
            key=key, type=type, default=default, doc=doc, operator_only=operator_only
        )

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

    def secrets_on_default(self) -> list[str]:
        """Keys declared secret=True with a default= and no stored value —
        i.e. secrets that are currently resolving to a literal from source
        code. Names only, never the defaults. Called at the end of boot()
        so an operator who forgot `arc settings set <key> --secret` hears
        about it instead of running on the dev placeholder."""
        return sorted(
            key
            for key, spec in self._declared_specs.items()
            if spec.default is not None
            and store.is_secret(self._conn, key)
            and not self._secret_has_value(key)
        )

    def _secret_has_value(self, key: str) -> bool:
        if self.secrets_provider() == "env":
            return bool(os.environ.get(env_secret_name(key)))
        return store.secret_has_value(self._conn, key)

    def secrets_provider(self) -> str:
        """The configured [secrets].provider — 'local_file' when unset (§3.5). Raises
        SettingsError for one ARC does not have (see SECRETS_PROVIDERS)."""
        doc = self._read_toml()
        secrets_table = doc.get("secrets", {})
        return _validated_provider(str(secrets_table.get("provider", "local_file")), self.toml_path)

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
        `value` is always None here — only callers using
        get(key, reveal=True) ever see a real secret value."""
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
            code_doc = spec.doc if spec else ""
            out[str(key)] = {
                "value": value,
                "kind": "secret" if is_secret_key else "setting",
                "type": spec.type.__name__ if spec and spec.type else (row["type"] if row else None),
                "default": spec.default if spec else None,
                # what the admin page shows: the copy kept in the store (the code's text copied in at boot, or an
                # administrator's own), and until the first copy the code's text itself
                "doc": (row["description"] if row and row["description"] else code_doc),
                "code_doc": code_doc,
                "operator_only": bool(spec and spec.operator_only),
                "doc_edited": bool(row and row["description_edited"]),
                "doc_by": row["description_by"] if row else None,
                "doc_at": row["description_at"] if row else None,
            }
        return out

    # ------------------------------------------------------------------ #
    # Descriptions — what each setting or secret is for. The code declares the original (declare(doc=...)); the
    # store keeps a copy per key, and an edit changes only that copy, never the code.
    # ------------------------------------------------------------------ #
    def sync_descriptions(self) -> int:
        """Copy every declared description into the store (see store.sync_descriptions). Runs when ARC starts. A
        store that cannot be written (read-only, locked for too long) is not a reason to stop: the admin page then
        shows the code's text, which is the same words. Returns how many rows it wrote."""
        docs = {key: spec.doc for key, spec in self._declared_specs.items() if spec.doc.strip()}
        try:
            return store.sync_descriptions(self._conn, docs)
        except sqlite3.Error as exc:
            _logger.warning("could not copy the settings descriptions into the store (%s: %s)", type(exc).__name__, exc)
            return 0

    def set_description(self, key: str, text: str, by: str | None = None) -> None:
        """An administrator's description for `key`, in place of the code's. Never changes the code, the value, or
        who set the value. Raises SettingsError for an empty text (use reset_description to go back), one over
        store.MAX_DESCRIPTION_CHARS, or a key that does not exist."""
        text = self._clean_description(key, text)
        if not store.set_description(self._conn, key, text, by, create=key in self._declared_specs):
            raise SettingsError(f"there is no setting or secret '{key}'.")

    @staticmethod
    def _clean_description(key: str, text: str) -> str:
        text = text.strip()
        if not text:
            raise SettingsError(f"the description of '{key}' cannot be empty; reset it to bring back the original instead.")
        if len(text) > store.MAX_DESCRIPTION_CHARS:
            raise SettingsError(
                f"the description of '{key}' is {len(text)} characters; the most allowed is {store.MAX_DESCRIPTION_CHARS}."
            )
        return text

    def reset_description(self, key: str) -> None:
        """Put back the text the code declares for `key` (none, for a key the code does not declare)."""
        spec = self._declared_specs.get(key)
        original = spec.doc if spec and spec.doc.strip() else None
        if not store.reset_description(self._conn, key, original, create=key in self._declared_specs):
            raise SettingsError(f"there is no setting or secret '{key}'.")

    def record_secret_access(self, key: str, accessed_by: str | None) -> None:
        """Write one audit row saying `accessed_by` was shown secret `key`.
        For the code path that surfaces a secret to a person (the Admin UI's
        reveal action) — never for code that merely uses one. Rejects a key
        that is not a stored secret, so the log can't fill with rows for
        values nobody was actually shown."""
        if not self._secret_has_value(key):
            raise SettingsError(f"'{key}' has no stored secret value to record access to.")
        store.record_access(self._conn, key, accessed_by)

    def access_log(self, key: str | None = None, limit: int = 100) -> list[dict]:
        """Recent secret_access_log rows — one per record_secret_access()
        call (a person being shown a secret), most recent first."""
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


def get(key: str, reveal: bool = False) -> Any:
    return _bound_manager().get(key, reveal=reveal)


def record_secret_access(key: str, accessed_by: str | None) -> None:
    return _bound_manager().record_secret_access(key, accessed_by)


def set(  # noqa: A001 - deliberate API name
    key: str,
    value: str,
    secret: bool = False,
    updated_by: str | None = None,
    type: str | None = None,  # deliberate API name, see SettingsManager.set
    description: str | None = None,
    operator: bool = False,
) -> None:
    return _bound_manager().set(
        key,
        value,
        secret=secret,
        updated_by=updated_by,
        type=type,
        description=description,
        operator=operator,
    )


def delete(key: str, operator: bool = False) -> bool:
    return _bound_manager().delete(key, operator=operator)


def is_operator_only(key: str) -> bool:
    return _bound_manager().is_operator_only(key)


def set_description(key: str, text: str, by: str | None = None) -> None:
    return _bound_manager().set_description(key, text, by)


def reset_description(key: str) -> None:
    return _bound_manager().reset_description(key)


def declare(
    key: str,
    secret: bool = False,
    *,
    type: type | None = None,  # noqa: A002 - deliberate API name, see SettingsManager.declare
    default: Any = None,
    doc: str = "",
    operator_only: bool = False,
) -> None:
    return _bound_manager().declare(
        key, secret=secret, type=type, default=default, doc=doc, operator_only=operator_only
    )


def is_secret(key: str) -> bool:
    return _bound_manager().is_secret(key)


def list_all() -> dict:
    return _bound_manager().list_all()


def access_log(key: str | None = None, limit: int = 100) -> list[dict]:
    return _bound_manager().access_log(key=key, limit=limit)
