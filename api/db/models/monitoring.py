"""
Forecast persistence and monitoring storage.

Six tables, which exist for one reason: a forecast cannot be scored until the
actual outcome is known, so the forecast has to be written down at the moment
it is made. Everything else in monitoring follows from joining those two
records.

* ``forecast_requests`` / ``forecasts`` -- what was predicted, and by whom
* ``actuals``                           -- what happened
* ``monitoring_metrics``                -- accuracy over a window
* ``drift_metrics``                     -- per-feature distribution shift
* ``alerts``                            -- thresholds that were crossed

Indexes are shaped around the three real queries: "this store's recent days",
"this model version's forecasts for a date", and "open alerts".
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, Optional

from sqlalchemy import (
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db.base import JSON_TYPE, TIMESTAMP_TYPE, Base, as_utc, utcnow
from api.db.models.user import USERS_TABLE

FORECAST_REQUESTS_TABLE = "forecast_requests"
FORECASTS_TABLE = "forecasts"
ACTUALS_TABLE = "actuals"
MONITORING_METRICS_TABLE = "monitoring_metrics"
DRIFT_METRICS_TABLE = "drift_metrics"
ALERTS_TABLE = "alerts"


class AlertSeverity(str, Enum):
    """How urgently an alert needs attention."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(str, Enum):
    """An alert's lifecycle."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class AlertType(str, Enum):
    """What triggered an alert."""

    ACCURACY_DEGRADATION = "accuracy_degradation"
    FEATURE_DRIFT = "feature_drift"
    PREDICTION_DRIFT = "prediction_drift"
    STALE_DATA = "stale_data"
    INTERVAL_COVERAGE = "interval_coverage"
    BIAS = "bias"


class DriftStatus(str, Enum):
    """A drift verdict."""

    STABLE = "stable"
    WARNING = "warning"
    DRIFT_DETECTED = "drift_detected"


class HealthStatus(str, Enum):
    """Overall monitoring verdict. ``NO_DATA`` is a first-class answer."""

    HEALTHY = "healthy"
    WARNING = "warning"
    DEGRADED = "degraded"
    NO_DATA = "no_data"


class ForecastRequestRecord(Base):
    """
    One ``POST /v1/forecast`` call.

    Separate from ``forecasts`` so the request's parameters are stored once
    rather than repeated on all ninety daily rows, and so a request is
    attributable even if its points are later pruned.
    """

    __tablename__ = FORECAST_REQUESTS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    forecast_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    store_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_level: Mapped[float] = mapped_column(Float, nullable=False)

    history_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    history_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    history_days: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Uncertainty method, imputed features, lineage -- everything needed to
    #: explain the numbers later without re-deriving them.
    forecast_metadata: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow, index=True
    )
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey(f"{USERS_TABLE}.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)

    points: Mapped[list["ForecastPoint"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_forecast_requests_store_created", "store_id", "created_at"),
        Index("ix_forecast_requests_version_created", "model_version", "created_at"),
    )


class ForecastPoint(Base):
    """One forecast day. Scored against ``actuals`` once the day has passed."""

    __tablename__ = FORECASTS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    forecast_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(f"{FORECAST_REQUESTS_TABLE}.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    store_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    forecast_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: The horizon step this point was: day 1 of 30, day 30 of 30. Accuracy
    #: degrades with horizon, so a fair comparison has to group by it.
    horizon_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    predicted_sales: Mapped[float] = mapped_column(Float, nullable=False)
    lower_bound: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    upper_bound: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow
    )
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey(f"{USERS_TABLE}.id", ondelete="SET NULL"), nullable=True
    )

    request: Mapped[ForecastRequestRecord] = relationship(back_populates="points")

    __table_args__ = (
        # The monitoring join: forecasts for a store on a date, by version.
        Index("ix_forecasts_store_date", "store_id", "forecast_date"),
        Index("ix_forecasts_version_date", "model_version", "forecast_date"),
        Index("ix_forecasts_date_created", "forecast_date", "created_at"),
    )


class Actual(Base):
    """
    An observed outcome for one store-day.

    Unique on (store, date): a second observation for the same day is a
    correction, applied as an update rather than inserted as a duplicate --
    otherwise every accuracy metric would double-count it.
    """

    __tablename__ = ACTUALS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    store_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    actual_sales: Mapped[float] = mapped_column(Float, nullable=False)

    #: Where the number came from: `training_dataset`, `manual`, `etl`.
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow, onupdate=utcnow
    )
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey(f"{USERS_TABLE}.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("store_id", "date", name="uq_actuals_store_date"),
        Index("ix_actuals_store_date", "store_id", "date"),
    )


class MonitoringMetric(Base):
    """One accuracy metric for one model version over one evaluation window."""

    __tablename__ = MONITORING_METRICS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    model_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: NULL means "across all stores", which is the headline figure.
    store_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    metric_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    #: How many scored store-days produced this value. A WAPE over 3 days and
    #: one over 300 are not comparable, so the count travels with the metric.
    sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    evaluation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow
    )

    __table_args__ = (
        # Re-running the monitoring DAG for a date must update, not duplicate.
        UniqueConstraint(
            "model_version",
            "store_id",
            "metric_name",
            "evaluation_date",
            "window_start",
            "window_end",
            name="uq_monitoring_metric_window",
        ),
        Index("ix_monitoring_metrics_version_date", "model_version", "evaluation_date"),
        Index("ix_monitoring_metrics_name_date", "metric_name", "evaluation_date"),
    )


class DriftMetric(Base):
    """One feature's distribution shift between a reference and current window."""

    __tablename__ = DRIFT_METRICS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    model_version: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    feature_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    drift_method: Mapped[str] = mapped_column(String(32), nullable=False)
    drift_score: Mapped[float] = mapped_column(Float, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: Window sizes, so a score computed from 20 points is not read as
    #: confidently as one from 2,000.
    reference_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    evaluation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "model_version",
            "feature_name",
            "drift_method",
            "evaluation_date",
            name="uq_drift_metric_evaluation",
        ),
        Index("ix_drift_metrics_version_date", "model_version", "evaluation_date"),
    )


class Alert(Base):
    """
    A threshold crossing that needs a human decision.

    ``fingerprint`` is what stops the daily DAG opening a new row for the same
    ongoing problem every run: an existing open alert with the same fingerprint
    is updated instead.
    """

    __tablename__ = ALERTS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    model_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    store_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AlertStatus.OPEN.value, index=True
    )

    #: Stable identity of the underlying condition, for deduplication.
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    #: How many evaluations have seen this condition. A problem on its tenth
    #: consecutive day is more urgent than the same problem on its first.
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: The numbers that triggered it, for the alert detail view.
    details: Mapped[Dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow, onupdate=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow
    )

    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP_TYPE, nullable=True)
    acknowledged_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey(f"{USERS_TABLE}.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP_TYPE, nullable=True)
    resolved_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey(f"{USERS_TABLE}.id", ondelete="SET NULL"), nullable=True
    )
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The alerts panel query: open alerts, newest first.
        Index("ix_alerts_status_created", "status", "created_at"),
        Index("ix_alerts_fingerprint_status", "fingerprint", "status"),
    )

    @property
    def is_open(self) -> bool:
        """Whether this alert still needs attention."""
        return self.status != AlertStatus.RESOLVED.value


__all__ = [
    "ACTUALS_TABLE",
    "ALERTS_TABLE",
    "DRIFT_METRICS_TABLE",
    "FORECASTS_TABLE",
    "FORECAST_REQUESTS_TABLE",
    "MONITORING_METRICS_TABLE",
    "Actual",
    "Alert",
    "AlertSeverity",
    "AlertStatus",
    "AlertType",
    "DriftMetric",
    "DriftStatus",
    "ForecastPoint",
    "ForecastRequestRecord",
    "HealthStatus",
    "MonitoringMetric",
]
