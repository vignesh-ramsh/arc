"""
arc.kernel.config
================
Loads ``arc.toml`` into a frozen :class:`ArcConfig`.

Env overrides: ``ARC_<SECTION>_<KEY>`` wins over the file value.
Example: ``ARC_APP_ENVIRONMENT=production``

New in Phase 2b+:
    [locale]    timezone, date_format, currency, decimal_places, float_precision
    [server]    host, port, workers, reload, access_log
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arc.kernel.exceptions import ConfigError


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str = "arc-app"
    version: str = "0.1.0"
    environment: str = "development"   # development | staging | production
    debug: bool = False
    secret_key: str = ""               # used by auth/session plugins


class LogConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    level: str = "INFO"                # DEBUG | INFO | WARNING | ERROR | CRITICAL
    renderer: str = "console"          # console (dev) | json (prod)
    include_timestamp: bool = True


class ServerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 1
    reload: bool = False               # never true in production
    access_log: bool = True


class LocaleConfig(BaseModel):
    """
    Locale / formatting preferences.

    timezone        IANA timezone name used for display. Storage is ALWAYS UTC.
                    Example: "Asia/Kolkata", "America/New_York", "Europe/London".
    date_format     strftime pattern for date display.
    datetime_format strftime pattern for datetime display.
    currency        ISO 4217 currency code (display only — no FX conversion).
    currency_symbol Symbol placed before amounts (display only).
    decimal_places  Default decimal places for Decimal fields with no explicit precision.
    float_precision Significant digits for Float fields.
    """
    model_config = ConfigDict(frozen=True)
    timezone: str = "UTC"
    date_format: str = "%Y-%m-%d"
    datetime_format: str = "%Y-%m-%d %H:%M:%S"
    currency: str = "USD"
    currency_symbol: str = "$"
    decimal_places: int = 2
    float_precision: int = 6


class ArcConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    app: AppConfig = Field(default_factory=AppConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    locale: LocaleConfig = Field(default_factory=LocaleConfig)
    # Per-plugin config tables: { "db": {...}, "api": {...}, "hrms": {...} }
    plugins: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def for_plugin(self, name: str) -> dict[str, Any]:
        """Return the [plugins.<name>] table (empty dict if absent)."""
        return dict(self.plugins.get(name, {}))


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """ARC_<SECTION>_<KEY>=value overrides the matching arc.toml field."""
    for key, value in os.environ.items():
        if not key.startswith("ARC_"):
            continue
        parts = key[4:].lower().split("_", 1)
        if len(parts) != 2:
            continue
        section, field = parts
        data.setdefault(section, {})
        if isinstance(data[section], dict):
            data[section][field] = value
    return data


def load_config(path: Path | None = None) -> ArcConfig:
    """Load arc.toml from *path*; return defaults if no file is found."""
    data: dict[str, Any] = {}
    if path is not None and path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"arc.toml at '{path}' is not valid TOML: {exc}",
                code="arc.config.invalid_toml",
            ) from exc

    data = _apply_env_overrides(data)

    try:
        return ArcConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(
            f"arc.toml failed validation: {exc}", code="arc.config.invalid"
        ) from exc