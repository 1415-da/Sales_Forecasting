"""
The declarative base and shared column types.

One metadata registry for every table in the application, so a single
``create_all`` (and a single Alembic ``target_metadata``) covers the whole
schema.

Historically ``Base`` lived in ``api.services.forecast_checkpoint_service``
because that module defined the first table. It now lives here and that module
re-exports it, so existing imports keep working while new models import from
the obvious place.

Column types are chosen to be portable: ``Uuid``, ``DateTime(timezone=True)``
and a JSON/JSONB variant all render natively on PostgreSQL while still working
on SQLite, which is what lets the whole schema be exercised in tests without a
database server.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

#: JSONB on PostgreSQL, plain JSON elsewhere.
JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")

#: Timezone-aware timestamps everywhere. A naive column would silently store
#: whatever the writing process's local time was.
TIMESTAMP_TYPE = DateTime(timezone=True)


class Base(DeclarativeBase):
    """Declarative base shared by every model in the application."""


def utcnow() -> datetime:
    """
    Current time as a timezone-aware UTC datetime.

    Used as a column default rather than ``func.now()`` so the value is
    identical on PostgreSQL and SQLite and does not depend on the server's
    timezone setting.
    """
    return datetime.now(timezone.utc)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """
    Coerce a stored timestamp to timezone-aware UTC.

    Necessary because the two supported backends disagree: PostgreSQL returns
    an aware datetime from a ``TIMESTAMPTZ`` column, while SQLite has no
    timezone type and hands back a naive one. Comparing the two raises
    ``TypeError``, so every comparison against a stored timestamp goes through
    here. A naive value is assumed to be UTC, which is true because every write
    path uses ``utcnow``.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["JSON_TYPE", "TIMESTAMP_TYPE", "Base", "as_utc", "utcnow"]
