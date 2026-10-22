"""
Engine and session management.

One engine per process, built on first use, shared by every repository. A
second engine would mean a second connection pool against the same database for
no benefit.

``get_session`` is the FastAPI dependency: it opens a session per request,
commits on success, rolls back on any exception, and always closes. That is
what makes a route either fully applied or fully absent, rather than partially
written when something fails halfway through.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from api.core.errors import DatabaseUnavailableError
from api.db.base import Base

logger = logging.getLogger(__name__)

#: Checked in order. `SCENARIO_DATABASE_URL` comes first because it is what the
#: existing deployment sets; `DATABASE_URL` is the conventional name.
DATABASE_URL_ENV_VARS = ("SCENARIO_DATABASE_URL", "DATABASE_URL")

_engine: Optional[Any] = None
_session_factory: Optional[sessionmaker] = None


def database_url() -> str:
    """
    The configured database URL.

    An unset URL is a configuration error, not a silent fallback to some
    default host: connecting somewhere unintended is worse than failing.
    """
    for env_var in DATABASE_URL_ENV_VARS:
        value = os.getenv(env_var)
        if value and value.strip():
            return value.strip()
    raise DatabaseUnavailableError(
        "No database URL is configured.",
        details={"expected_env_vars": list(DATABASE_URL_ENV_VARS)},
    )


def get_engine() -> Any:
    """The process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        url = database_url()
        try:
            # `pool_pre_ping` because a pooled connection can be dropped by the
            # database or an intermediary while idle; without it the first query
            # after an idle period fails instead of transparently reconnecting.
            _engine = create_engine(url, pool_pre_ping=True, future=True)
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(
                "The database engine could not be created.",
                details={"error": str(exc)},
            ) from exc
    return _engine


def get_session_factory() -> sessionmaker:
    """Session factory bound to the process-wide engine."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _session_factory


def reset_engine() -> None:
    """
    Drop the cached engine and session factory.

    For tests that point the process at a different database, and for a
    configuration reload. Disposes the old pool so its connections are closed
    rather than leaked.
    """
    global _engine, _session_factory
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception as exc:  # noqa: BLE001 - disposal is best-effort
            logger.debug("Engine disposal failed: %s", exc)
    _engine = None
    _session_factory = None


def create_all() -> None:
    """
    Create any missing tables.

    Alembic owns the schema in a real deployment; this exists so tests and a
    first local run work without a migration step. It only ever adds tables,
    never alters or drops one.
    """
    try:
        Base.metadata.create_all(get_engine())
    except SQLAlchemyError as exc:
        raise DatabaseUnavailableError(
            "The database schema could not be created.",
            details={"error": str(exc)},
        ) from exc


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    A transactional session, for code outside a request (DAGs, bootstrap).

    Commits on success and rolls back on any exception, so a partially applied
    write is never left behind.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """
    FastAPI dependency yielding a per-request transactional session.

    Not decorated with ``@contextmanager``: FastAPI drives the generator itself
    and needs the raw generator protocol.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


#: Nullable columns added after a table first shipped. ``create_all`` never
#: alters an existing table, so a database created before one of these exists
#: gets it here; each also has an Alembic revision for migrated deployments.
ADDITIVE_COLUMNS = (
    ("scenario_baseline_forecasts", "model", "VARCHAR(32)"),
    # Training-data ingestion (adapters, mapping, background processing).
    ("training_datasets", "source_files", "JSONB"),
    ("training_datasets", "adapter_id", "VARCHAR(64)"),
    ("training_datasets", "adapter_version", "VARCHAR(16)"),
    ("training_datasets", "ingestion_config", "JSONB"),
    ("training_datasets", "mapping_version", "INTEGER"),
    ("training_datasets", "detection", "JSONB"),
    ("training_datasets", "source_namespace", "VARCHAR(64)"),
    ("training_datasets", "target_type", "VARCHAR(16)"),
    ("training_datasets", "source_grain", "VARCHAR(32)"),
    ("training_datasets", "feature_options", "JSONB"),
    ("training_datasets", "context_object_key", "VARCHAR(255)"),
    ("training_datasets", "context_object_sha256", "VARCHAR(64)"),
    ("training_datasets", "acknowledged_warnings", "JSONB"),
    ("training_datasets", "upload_key", "VARCHAR(64)"),
    ("training_datasets", "processing_error", "TEXT"),
    ("training_datasets", "processing_started_at", "TIMESTAMP WITH TIME ZONE"),
    ("training_datasets", "processing_finished_at", "TIMESTAMP WITH TIME ZONE"),
)


#: Indexes that accompany ``ADDITIVE_COLUMNS`` (same Alembic revisions).
ADDITIVE_INDEXES = (
    ("uq_training_datasets_upload_key", "training_datasets", "upload_key", True),
)


def ensure_additive_columns() -> None:
    """Add any missing nullable column from ``ADDITIVE_COLUMNS``. Only ever adds."""
    from sqlalchemy import inspect, text

    engine = get_engine()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, column, ddl_type in ADDITIVE_COLUMNS:
        if table not in tables:
            continue
        if column in {item["name"] for item in inspector.get_columns(table)}:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        logger.info("Added column %s.%s", table, column)
    for name, table, column, unique in ADDITIVE_INDEXES:
        if table not in tables:
            continue
        existing = {item["name"] for item in inspect(engine).get_indexes(table)}
        if name in existing:
            continue
        with engine.begin() as connection:
            connection.execute(
                text(f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS {name} ON {table} ({column})")
            )
        logger.info("Created index %s", name)


def ensure_tables_once() -> None:
    """
    Create missing tables and additive columns, swallowing failure.

    Called at application start so a fresh local database works immediately.
    A failure here must not stop the process: the health endpoint reports the
    database as unavailable, which is more useful than a crash loop.
    """
    try:
        create_all()
        ensure_additive_columns()
    except Exception as exc:  # noqa: BLE001 - startup must not fail on this
        logger.warning("Could not ensure database schema at startup: %s", exc)


__all__ = [
    "DATABASE_URL_ENV_VARS",
    "create_all",
    "database_url",
    "ensure_tables_once",
    "get_engine",
    "get_session",
    "get_session_factory",
    "reset_engine",
    "session_scope",
]
