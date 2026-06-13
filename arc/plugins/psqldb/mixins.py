"""
arc.plugins.psqldb.mixins
====================
System fields injected into every Arc table. These mixins document and (for
ORM use) declare the columns the schema compiler emits automatically. They
must never be declared in schema JSON.

| Field      | Type        | Managed by                       |
|------------|-------------|----------------------------------|
| id         | UUID        | DB (gen_random_uuid())           |
| created_at | TIMESTAMPTZ | DB (now())                       |
| updated_at | TIMESTAMPTZ | DB trigger arc_bump_version()    |
| created_by | VARCHAR     | ORM listener (UserContext)       |
| updated_by | VARCHAR     | ORM listener (UserContext)       |
| _state     | INTEGER     | user / workflow engine (default 0)|
| version    | INTEGER     | DB trigger (default 1)           |
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class AuditMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    _state: Mapped[int] = mapped_column(Integer, server_default="0")


class VersionMixin:
    version: Mapped[int] = mapped_column(Integer, server_default="1")
