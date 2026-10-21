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


def ensure_tables_once() -> None:
    """
    Create missing tables, swallowing failure.

    Called at application start so a fresh local database works immediately.
    A failure here must not stop the process: the health endpoint reports the
    database as unavailable, which is more useful than a crash loop.
    """
    try:
        create_all()
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
