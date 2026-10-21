"""
Persistence for baseline forecast checkpoints.

Owns the ``scenario_baseline_forecasts`` table and the status transitions that
make baseline generation rollback safe: a row is written with status ``created``
*before* inference starts, then moved to ``completed`` or ``failed``. A row is
never marked ``completed`` until the full forecast exists.

The column types are portable: SQLAlchemy's ``Uuid`` and ``DateTime(timezone=True)``
render natively on PostgreSQL and still work on SQLite, so this service can be
exercised against a real schema in tests without a database server.

Only metadata is stored. Forecast points are intentionally not persisted yet --
see the module note in ``baseline_forecast_service``.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy import Date, DateTime, Integer, String, Uuid, create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from api.db.base import Base as _Base

from api.schemas.baseline_forecast import (
    BaselineForecastCheckpoint,
    BaselineForecastErrorResponse,
    BaselineForecastStatus,
)

logger = logging.getLogger(__name__)

TABLE_NAME = "scenario_baseline_forecasts"
DATABASE_URL_ENV_VARS = ("SCENARIO_DATABASE_URL", "DATABASE_URL")
TTL_HOURS_ENV_VAR = "BASELINE_FORECAST_TTL_HOURS"
DEFAULT_TTL_HOURS = 24

ERROR_MESSAGE_MAX_LENGTH = 2000


class CheckpointErrorCode(str, Enum):
    """Machine-readable codes for checkpoint persistence failures."""

    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    DATABASE_NOT_CONFIGURED = "DATABASE_NOT_CONFIGURED"
    CHECKPOINT_NOT_FOUND = "CHECKPOINT_NOT_FOUND"


class CheckpointError(Exception):
    """
    A checkpoint could not be read or written.

    Mirrors the ``error_code`` / ``message`` / ``field`` / ``details`` shape used
    by the other service errors so a route layer can handle them uniformly.
    """

    def __init__(
        self,
        error_code: CheckpointErrorCode | str,
        message: str,
        field: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_code: str = (
            error_code.value if isinstance(error_code, CheckpointErrorCode) else error_code
        )
        self.message = message
        self.field = field
        self.details: Dict[str, Any] = details or {}
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict form of the error."""
        return {
            "error_code": self.error_code,
            "message": self.message,
            "field": self.field,
            "details": self.details,
        }

    def to_response(self) -> BaselineForecastErrorResponse:
        """Schema form, ready to serialise as an error body."""
        return BaselineForecastErrorResponse(**self.to_dict())

    def __repr__(self) -> str:
        return (
            f"CheckpointError(error_code={self.error_code!r}, "
            f"field={self.field!r}, message={self.message!r})"
        )


# The declarative base now lives in `api.db.base` so every table in the
# application shares one metadata registry (and one Alembic target). It is
# re-exported here because this module defined the first table and existing
# code imports `Base` from it.
Base = _Base


class ScenarioBaselineForecast(Base):
    """One baseline forecast checkpoint."""

    __tablename__ = TABLE_NAME

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    forecast_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    store_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    history_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    history_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    forecast_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    forecast_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    data_source: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Required by the rollback-safe contract: a failed run must record *why*.
    # Additive to the specified column list, both nullable.
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(
        String(ERROR_MESSAGE_MAX_LENGTH), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"ScenarioBaselineForecast(forecast_id={self.forecast_id!r}, "
            f"status={self.status!r}, store_id={self.store_id!r})"
        )


def resolve_database_url(explicit_url: Optional[str] = None) -> str:
    """
    Resolve the database URL from the argument or environment.

    Credentials are never hardcoded; an unset URL is a configuration error, not
    a silent fallback to some default host.
    """
    if explicit_url:
        return explicit_url
    for env_var in DATABASE_URL_ENV_VARS:
        value = os.getenv(env_var)
        if value:
            return value
    raise CheckpointError(
        CheckpointErrorCode.DATABASE_NOT_CONFIGURED,
        "No database URL configured for baseline forecast checkpoints. "
        f"Set one of: {', '.join(DATABASE_URL_ENV_VARS)}.",
        field="database_url",
        details={"env_vars": list(DATABASE_URL_ENV_VARS)},
    )


def default_ttl() -> Optional[timedelta]:
    """Checkpoint time-to-live, or None when expiry is disabled."""
    raw = os.getenv(TTL_HOURS_ENV_VAR)
    if raw is None:
        return timedelta(hours=DEFAULT_TTL_HOURS)
    try:
        hours = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; falling back to %s hours", TTL_HOURS_ENV_VAR, raw, DEFAULT_TTL_HOURS
        )
        return timedelta(hours=DEFAULT_TTL_HOURS)
    return timedelta(hours=hours) if hours > 0 else None


def new_forecast_id() -> str:
    """Opaque, URL-safe baseline forecast identifier."""
    from api.schemas.baseline_forecast import BASELINE_FORECAST_ID_PREFIX

    return f"{BASELINE_FORECAST_ID_PREFIX}{uuid.uuid4().hex}"


class ForecastCheckpointService:
    """
    Reads and writes baseline forecast checkpoints.

    Pass ``engine`` or ``database_url``; the engine is created lazily so
    constructing the service never opens a connection. Every SQLAlchemy failure
    is converted to ``CheckpointError`` so callers never see driver exceptions.
    """

    def __init__(
        self,
        engine: Optional[Any] = None,
        database_url: Optional[str] = None,
        ttl: Optional[timedelta] = None,
        create_tables: bool = False,
    ) -> None:
        self._engine = engine
        self._database_url = database_url
        self._session_factory: Optional[sessionmaker] = None
        self.ttl = ttl if ttl is not None else default_ttl()

        if create_tables:
            self.create_all()

    @property
    def engine(self) -> Any:
        """The SQLAlchemy engine, created on first use."""
        if self._engine is None:
            url = resolve_database_url(self._database_url)
            try:
                self._engine = create_engine(url, pool_pre_ping=True, future=True)
            except SQLAlchemyError as exc:
                raise self._unavailable("create database engine", exc) from exc
        return self._engine

    @property
    def session_factory(self) -> sessionmaker:
        """Session factory bound to the engine."""
        if self._session_factory is None:
            self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        return self._session_factory

    def create_all(self) -> None:
        """Create the checkpoint table when it does not exist."""
        try:
            Base.metadata.create_all(self.engine)
        except SQLAlchemyError as exc:
            raise self._unavailable("create checkpoint table", exc) from exc

    @staticmethod
    def _unavailable(action: str, exc: Exception) -> CheckpointError:
        """Wrap a driver failure as a structured, loggable error."""
        logger.error("Checkpoint database error while trying to %s: %s", action, exc)
        return CheckpointError(
            CheckpointErrorCode.DATABASE_UNAVAILABLE,
            f"Checkpoint database is unavailable (failed to {action}).",
            field="database",
            details={"action": action, "error": str(exc)},
        )

    def create(
        self,
        *,
        store_id: str,
        model_version: str,
        model_stage: str,
        horizon_days: int,
        history_start_date: date,
        history_end_date: date,
        forecast_start_date: date,
        forecast_end_date: date,
        data_source: str,
        dataset_version: str,
        feature_schema_version: str,
        forecast_id: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> BaselineForecastCheckpoint:
        """Insert a checkpoint with status ``created``, before any inference runs."""
        now = datetime.now(timezone.utc)
        if expires_at is None and self.ttl is not None:
            expires_at = now + self.ttl

        row = ScenarioBaselineForecast(
            id=uuid.uuid4(),
            forecast_id=forecast_id or new_forecast_id(),
            store_id=store_id,
            model_version=model_version,
            model_stage=model_stage,
            horizon_days=horizon_days,
            history_start_date=history_start_date,
            history_end_date=history_end_date,
            forecast_start_date=forecast_start_date,
            forecast_end_date=forecast_end_date,
            data_source=data_source,
            dataset_version=dataset_version,
            feature_schema_version=feature_schema_version,
            status=BaselineForecastStatus.CREATED.value,
            created_at=now,
            expires_at=expires_at,
        )

        try:
            with self.session_factory() as session:
                session.add(row)
                session.commit()
                return self._to_schema(row)
        except SQLAlchemyError as exc:
            raise self._unavailable("insert checkpoint", exc) from exc

    def mark_completed(self, forecast_id: str) -> BaselineForecastCheckpoint:
        """Move a checkpoint to ``completed``; called only after a full forecast."""
        return self._update_status(forecast_id, BaselineForecastStatus.COMPLETED)

    def mark_failed(
        self,
        forecast_id: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> BaselineForecastCheckpoint:
        """Move a checkpoint to ``failed`` and record why."""
        return self._update_status(
            forecast_id,
            BaselineForecastStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
        )

    def mark_expired(self, forecast_id: str) -> BaselineForecastCheckpoint:
        """Move a checkpoint to ``expired``."""
        return self._update_status(forecast_id, BaselineForecastStatus.EXPIRED)

    def _update_status(
        self,
        forecast_id: str,
        status: BaselineForecastStatus,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> BaselineForecastCheckpoint:
        """Apply a status transition to one checkpoint."""
        try:
            with self.session_factory() as session:
                row = self._fetch_row(session, forecast_id)
                if row is None:
                    raise CheckpointError(
                        CheckpointErrorCode.CHECKPOINT_NOT_FOUND,
                        f"Baseline forecast '{forecast_id}' was not found.",
                        field="baseline_forecast_id",
                        details={"forecast_id": forecast_id},
                    )
                row.status = status.value
                if error_code is not None:
                    row.error_code = error_code
                if error_message is not None:
                    row.error_message = error_message[:ERROR_MESSAGE_MAX_LENGTH]
                session.commit()
                return self._to_schema(row)
        except SQLAlchemyError as exc:
            raise self._unavailable(f"update checkpoint to {status.value}", exc) from exc

    def get(self, forecast_id: str) -> Optional[BaselineForecastCheckpoint]:
        """Fetch one checkpoint, or None when it does not exist."""
        try:
            with self.session_factory() as session:
                row = self._fetch_row(session, forecast_id)
                return self._to_schema(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise self._unavailable("read checkpoint", exc) from exc

    def list_for_store(
        self, store_id: str, limit: int = 20
    ) -> List[BaselineForecastCheckpoint]:
        """Most recent checkpoints for a store, newest first."""
        try:
            with self.session_factory() as session:
                stmt = (
                    select(ScenarioBaselineForecast)
                    .where(ScenarioBaselineForecast.store_id == store_id)
                    .order_by(ScenarioBaselineForecast.created_at.desc())
                    .limit(limit)
                )
                return [self._to_schema(row) for row in session.scalars(stmt).all()]
        except SQLAlchemyError as exc:
            raise self._unavailable("list checkpoints", exc) from exc

    def expire_stale(self, now: Optional[datetime] = None) -> int:
        """Mark every past-expiry ``created``/``completed`` checkpoint as expired."""
        moment = now or datetime.now(timezone.utc)
        active = (BaselineForecastStatus.CREATED.value, BaselineForecastStatus.COMPLETED.value)
        try:
            with self.session_factory() as session:
                stmt = select(ScenarioBaselineForecast).where(
                    ScenarioBaselineForecast.status.in_(active),
                    ScenarioBaselineForecast.expires_at.is_not(None),
                    ScenarioBaselineForecast.expires_at <= moment,
                )
                rows = list(session.scalars(stmt).all())
                for row in rows:
                    row.status = BaselineForecastStatus.EXPIRED.value
                session.commit()
                return len(rows)
        except SQLAlchemyError as exc:
            raise self._unavailable("expire stale checkpoints", exc) from exc

    @staticmethod
    def _fetch_row(session: Session, forecast_id: str) -> Optional[ScenarioBaselineForecast]:
        """Load one row by its public forecast id."""
        stmt = select(ScenarioBaselineForecast).where(
            ScenarioBaselineForecast.forecast_id == forecast_id
        )
        return session.scalars(stmt).first()

    @staticmethod
    def _to_schema(row: ScenarioBaselineForecast) -> BaselineForecastCheckpoint:
        """Convert an ORM row to its transport schema."""
        return BaselineForecastCheckpoint.model_validate(row)
