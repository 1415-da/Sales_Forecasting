"""
SQLAlchemy models for persisted scenario forecasts.

Three tables, one metadata registry:

* ``scenario_forecasts``       -- one row per scenario comparison
* ``scenario_daily_forecasts`` -- the per-day baseline/scenario quantiles
* ``scenario_audit_events``    -- defined with the audit service and re-exported
  here so every scenario table is importable from one place

Column types are portable: ``Uuid``, ``DateTime(timezone=True)`` and a
JSON/JSONB variant all render natively on PostgreSQL while still working on
SQLite, so the schema can be exercised in tests without a database server.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

# One registry for every scenario table; see api/db/__init__ for why it lives there.
from api.services.forecast_checkpoint_service import Base
from api.services.scenario_audit_service import ScenarioAuditEvent

SCENARIO_FORECASTS_TABLE = "scenario_forecasts"
SCENARIO_DAILY_FORECASTS_TABLE = "scenario_daily_forecasts"

ERROR_MESSAGE_MAX_LENGTH = 4000

# JSONB on PostgreSQL, plain JSON elsewhere.
JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class ScenarioForecastStatus(str, Enum):
    """Lifecycle of a persisted scenario comparison."""

    CREATED = "created"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


#: Statuses that an expiry sweep may move to ``expired``.
EXPIRABLE_STATUSES = (
    ScenarioForecastStatus.CREATED.value,
    ScenarioForecastStatus.COMPLETED.value,
)


class ScenarioForecast(Base):
    """One scenario comparison, with its request and result payloads."""

    __tablename__ = SCENARIO_FORECASTS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scenario_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    baseline_forecast_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    store_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    scenario_type: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    forecast_type: Mapped[str] = mapped_column(String(32), nullable=False)
    request_payload: Mapped[Dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    response_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Attribution. `SET NULL` on delete and a denormalised email so a saved
    # scenario stays readable after the account that created it is removed.
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    daily_forecasts: Mapped[List["ScenarioDailyForecast"]] = relationship(
        back_populates="scenario",
        cascade="save-update, merge",
        order_by="ScenarioDailyForecast.forecast_date",
        lazy="selectin",
    )

    __table_args__ = (
        Index("ix_scenario_forecasts_store_created", "store_id", "created_at"),
        Index("ix_scenario_forecasts_status_expires", "status", "expires_at"),
        # "my saved scenarios, newest first" -- the saved-scenario list query.
        Index("ix_scenario_forecasts_creator_created", "created_by_id", "created_at"),
    )

    @property
    def is_expired(self) -> bool:
        """Whether this scenario has passed its expiry."""
        if self.expires_at is None:
            return False
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)

    @property
    def is_retrievable(self) -> bool:
        """Whether the stored comparison may still be served."""
        return (
            self.status == ScenarioForecastStatus.COMPLETED.value
            and not self.is_expired
            and bool(self.response_summary)
        )

    def __repr__(self) -> str:
        return (
            f"ScenarioForecast(scenario_id={self.scenario_id!r}, "
            f"status={self.status!r}, store_id={self.store_id!r})"
        )


class ScenarioDailyForecast(Base):
    """Per-day baseline and scenario quantiles for one comparison."""

    __tablename__ = SCENARIO_DAILY_FORECASTS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scenario_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(f"{SCENARIO_FORECASTS_TABLE}.scenario_id"),
        nullable=False,
        index=True,
    )
    forecast_date: Mapped[date] = mapped_column(Date, nullable=False)

    baseline_p10: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    baseline_p50: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    baseline_p90: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scenario_p10: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scenario_p50: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    scenario_p90: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    difference_p50: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    difference_pct_p50: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    scenario: Mapped[ScenarioForecast] = relationship(back_populates="daily_forecasts")

    __table_args__ = (
        Index(
            "ux_scenario_daily_forecasts_scenario_date",
            "scenario_id",
            "forecast_date",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return (
            f"ScenarioDailyForecast(scenario_id={self.scenario_id!r}, "
            f"forecast_date={self.forecast_date!r})"
        )


__all__ = [
    "EXPIRABLE_STATUSES",
    "JSON_TYPE",
    "SCENARIO_DAILY_FORECASTS_TABLE",
    "SCENARIO_FORECASTS_TABLE",
    "Base",
    "ScenarioAuditEvent",
    "ScenarioDailyForecast",
    "ScenarioForecast",
    "ScenarioForecastStatus",
]
