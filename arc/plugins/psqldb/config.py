"""
arc.plugins.psqldb.config
====================
``DatabaseConfig`` — resolved from the ``[plugins.db]`` table in arc.toml,
with the ``DATABASE_URL`` environment variable taking priority.

asyncpg only. URLs must use the ``postgresql+asyncpg://`` scheme.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str = ""
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout: float = 30.0
    pool_recycle: int = 1800
    echo: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "DatabaseConfig":
        merged: dict[str, Any] = dict(data)
        env_url = os.environ.get("DATABASE_URL")
        if env_url:
            merged["url"] = env_url
        return cls.model_validate(merged)
