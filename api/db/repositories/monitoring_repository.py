"""
Forecast and monitoring persistence.

Two repositories with different lifetimes:

* ``ForecastRepository`` is written on the request path, so it must never make
  a forecast fail. Its writes are the caller's to guard.
* ``MonitoringRepository`` is written by the monitoring DAG and read by the
  dashboard. Its upserts are keyed so re-running the DAG for a date corrects
  that date rather than duplicating it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.core.errors import DatabaseUnavailableError
from api.db.base import utcnow
from api.db.models.monitoring import (
    Actual,
    Alert,
    AlertStatus,
    DriftMetric,
    ForecastPoint,
    ForecastRequestRecord,
    MonitoringMetric,
)

logger = logging.getLogger(__name__)


class ForecastRepository:
    """Reads and writes ``forecast_requests`` and ``forecasts``."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def save_forecast(
        self,
        *,
        forecast_id: str,
        store_id: str,
        model_name: str,
        model_version: str,
        model_stage: str,
        run_id: Optional[str],
        horizon_days: int,
        confidence_level: float,
        history_start_date: date,
        history_end_date: date,
        history_days: int,
        metadata: Dict[str, Any],
        points: Sequence[Dict[str, Any]],
        created_by_id: Optional[uuid.UUID] = None,
        created_by_email: Optional[str] = None,
    ) -> ForecastRequestRecord:
        """
        Persist a forecast and its daily points.

        One request row plus N point rows, inserted together so a forecast is
        never half-recorded. ``horizon_step`` is stored per point because
        accuracy degrades with horizon and a fair comparison must group by it.
        """
        record = ForecastRequestRecord(
            forecast_id=forecast_id,
            store_id=store_id,
            model_name=model_name,
            model_version=model_version,
            model_stage=model_stage,
            run_id=run_id,
            horizon_days=horizon_days,
            confidence_level=confidence_level,
            history_start_date=history_start_date,
            history_end_date=history_end_date,
            history_days=history_days,
            forecast_metadata=dict(metadata or {}),
            created_by_id=created_by_id,
            created_by_email=(created_by_email.lower() if created_by_email else None),
        )
        self.session.add(record)
        self.session.flush()

        for step, point in enumerate(points, start=1):
            self.session.add(
                ForecastPoint(
                    forecast_request_id=record.id,
                    store_id=store_id,
                    model_name=model_name,
                    model_version=model_version,
                    forecast_date=point["date"],
                    horizon_step=step,
                    predicted_sales=float(point["predicted_sales"]),
                    lower_bound=_optional_float(point.get("lower_bound")),
                    upper_bound=_optional_float(point.get("upper_bound")),
                    confidence_level=confidence_level,
                    created_by_id=created_by_id,
                )
            )
        self.session.flush()
        return record

    def latest_forecast_date(self, *, store_id: Optional[str] = None) -> Optional[date]:
        """The furthest-out forecast day on record. Drives data freshness."""
        statement = select(func.max(ForecastPoint.forecast_date))
        if store_id:
            statement = statement.where(ForecastPoint.store_id == store_id)
        return self._scalar(statement, "read latest forecast date")

    def latest_forecast_created_at(self) -> Optional[datetime]:
        """When a forecast was last produced."""
        return self._scalar(
            select(func.max(ForecastRequestRecord.created_at)),
            "read latest forecast timestamp",
        )

    def count_forecasts(self) -> int:
        """How many forecast points exist. Distinguishes "no data" from "bad data"."""
        return int(
            self._scalar(select(func.count(ForecastPoint.id)), "count forecasts") or 0
        )

    def scored_pairs(
        self,
        *,
        window_start: date,
        window_end: date,
        model_version: Optional[str] = None,
        store_id: Optional[str] = None,
        horizon_step: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Forecast/actual pairs for a window, ready to score.

        Only days with both a forecast and an actual are returned, which is the
        definition of "scorable". Where several forecasts exist for the same
        store-day (a 30-day forecast re-run daily), the earliest-created one is
        kept so the series is not dominated by whichever run happened last.
        """
        filters = [
            ForecastPoint.forecast_date >= window_start,
            ForecastPoint.forecast_date <= window_end,
        ]
        if model_version:
            filters.append(ForecastPoint.model_version == model_version)
        if store_id:
            filters.append(ForecastPoint.store_id == store_id)
        if horizon_step is not None:
            filters.append(ForecastPoint.horizon_step == horizon_step)

        statement = (
            select(
                ForecastPoint.store_id,
                ForecastPoint.forecast_date,
                ForecastPoint.model_version,
                ForecastPoint.horizon_step,
                ForecastPoint.predicted_sales,
                ForecastPoint.lower_bound,
                ForecastPoint.upper_bound,
                ForecastPoint.created_at,
                Actual.actual_sales,
            )
            .join(
                Actual,
                and_(
                    Actual.store_id == ForecastPoint.store_id,
                    Actual.date == ForecastPoint.forecast_date,
                ),
            )
            .where(*filters)
            .order_by(ForecastPoint.forecast_date, ForecastPoint.created_at)
        )

        try:
            rows = list(self.session.execute(statement))
        except SQLAlchemyError as exc:
            raise self._unavailable("join forecasts with actuals", exc) from exc

        # Keep the first row per (store, date, version, step): the ordering
        # above makes that the earliest-created forecast.
        seen: set = set()
        pairs: List[Dict[str, Any]] = []
        for row in rows:
            key = (row[0], row[1], row[2], row[3])
            if key in seen:
                continue
            seen.add(key)
            pairs.append(
                {
                    "store_id": row[0],
                    "date": row[1],
                    "model_version": row[2],
                    "horizon_step": row[3],
                    "predicted": float(row[4]),
                    "lower_bound": _optional_float(row[5]),
                    "upper_bound": _optional_float(row[6]),
                    "created_at": row[7],
                    "actual": float(row[8]),
                }
            )
        return pairs

    def _scalar(self, statement, action: str):
        """Execute a scalar query, converting driver failures."""
        try:
            return self.session.execute(statement).scalar()
        except SQLAlchemyError as exc:
            raise self._unavailable(action, exc) from exc

    @staticmethod
    def _unavailable(action: str, exc: Exception) -> DatabaseUnavailableError:
        """Wrap a driver failure without leaking connection details."""
        logger.error("Forecast store error while trying to %s: %s", action, exc)
        return DatabaseUnavailableError(
            "The forecast store is currently unavailable.",
            details={"operation": action},
        )


class ActualRepository:
    """Reads and writes ``actuals``."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(
        self,
        rows: Iterable[Dict[str, Any]],
        *,
        source: str = "etl",
        created_by_id: Optional[uuid.UUID] = None,
    ) -> Tuple[int, int]:
        """
        Insert or correct actuals, returning ``(inserted, updated)``.

        A repeated store-day is a correction, applied as an update. Inserting it
        again would double-count the day in every accuracy metric.
        """
        inserted = 0
        updated = 0
        for row in rows:
            store_id = str(row["store_id"])
            observed_on = row["date"]
            value = float(row["actual_sales"])

            existing = self.session.execute(
                select(Actual).where(
                    Actual.store_id == store_id, Actual.date == observed_on
                )
            ).scalar_one_or_none()

            if existing is None:
                self.session.add(
                    Actual(
                        store_id=store_id,
                        date=observed_on,
                        actual_sales=value,
                        source=source,
                        created_by_id=created_by_id,
                    )
                )
                inserted += 1
            elif existing.actual_sales != value:
                existing.actual_sales = value
                existing.source = source
                existing.updated_at = utcnow()
                updated += 1

        self.session.flush()
        return inserted, updated

    def latest_date(self, *, store_id: Optional[str] = None) -> Optional[date]:
        """The most recent observed day. The data-freshness anchor."""
        statement = select(func.max(Actual.date))
        if store_id:
            statement = statement.where(Actual.store_id == store_id)
        return self.session.execute(statement).scalar()

    def coverage_by_store(self, *, since: Optional[date] = None) -> List[Dict[str, Any]]:
        """Per-store latest actual and day count, for the freshness card."""
        statement = select(
            Actual.store_id, func.max(Actual.date), func.count(Actual.id)
        ).group_by(Actual.store_id)
        if since is not None:
            statement = statement.where(Actual.date >= since)
        rows = self.session.execute(statement.order_by(Actual.store_id))
        return [
            {"store_id": row[0], "latest_date": row[1], "days": int(row[2])}
            for row in rows
        ]

    def count(self) -> int:
        """How many actuals exist."""
        return int(self.session.execute(select(func.count(Actual.id))).scalar() or 0)

    def series(
        self, *, store_id: str, start: date, end: date
    ) -> List[Tuple[date, float]]:
        """One store's observed series over a window, in date order."""
        statement = (
            select(Actual.date, Actual.actual_sales)
            .where(Actual.store_id == store_id, Actual.date >= start, Actual.date <= end)
            .order_by(Actual.date)
        )
        return [(row[0], float(row[1])) for row in self.session.execute(statement)]


class MonitoringRepository:
    """Reads and writes monitoring metrics, drift scores, and alerts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- metrics -----------------------------------------------------------

    def upsert_metric(
        self,
        *,
        model_version: str,
        metric_name: str,
        metric_value: float,
        evaluation_date: date,
        window_start: date,
        window_end: date,
        model_name: Optional[str] = None,
        store_id: Optional[str] = None,
        sample_size: Optional[int] = None,
    ) -> MonitoringMetric:
        """
        Record one metric, replacing any existing value for the same window.

        Keyed on the full window so re-running the DAG corrects a date instead
        of appending a second, conflicting figure for it.
        """
        existing = self.session.execute(
            select(MonitoringMetric).where(
                MonitoringMetric.model_version == model_version,
                MonitoringMetric.store_id.is_(None)
                if store_id is None
                else MonitoringMetric.store_id == store_id,
                MonitoringMetric.metric_name == metric_name,
                MonitoringMetric.evaluation_date == evaluation_date,
                MonitoringMetric.window_start == window_start,
                MonitoringMetric.window_end == window_end,
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.metric_value = float(metric_value)
            existing.sample_size = sample_size
            existing.model_name = model_name or existing.model_name
            self.session.flush()
            return existing

        metric = MonitoringMetric(
            model_version=model_version,
            model_name=model_name,
            store_id=store_id,
            metric_name=metric_name,
            metric_value=float(metric_value),
            sample_size=sample_size,
            evaluation_date=evaluation_date,
            window_start=window_start,
            window_end=window_end,
        )
        self.session.add(metric)
        self.session.flush()
        return metric

    def metric_series(
        self,
        *,
        metric_names: Sequence[str],
        model_version: Optional[str] = None,
        store_id: Optional[str] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
        limit: int = 400,
    ) -> List[MonitoringMetric]:
        """Metric history for the trend charts, oldest first."""
        filters = [MonitoringMetric.metric_name.in_(list(metric_names))]
        if model_version:
            filters.append(MonitoringMetric.model_version == model_version)
        if store_id:
            filters.append(MonitoringMetric.store_id == store_id)
        else:
            # The headline series is the all-stores aggregate, not an arbitrary
            # store's. Without this, a per-store row would be mixed in.
            filters.append(MonitoringMetric.store_id.is_(None))
        if start is not None:
            filters.append(MonitoringMetric.evaluation_date >= start)
        if end is not None:
            filters.append(MonitoringMetric.evaluation_date <= end)

        statement = (
            select(MonitoringMetric)
            .where(*filters)
            .order_by(MonitoringMetric.evaluation_date.asc())
            .limit(max(1, min(limit, 2000)))
        )
        return list(self.session.execute(statement).scalars())

    def latest_metrics(
        self, *, model_version: Optional[str] = None, store_id: Optional[str] = None
    ) -> Dict[str, MonitoringMetric]:
        """The newest value of each metric, keyed by name."""
        filters = []
        if model_version:
            filters.append(MonitoringMetric.model_version == model_version)
        if store_id:
            filters.append(MonitoringMetric.store_id == store_id)
        else:
            filters.append(MonitoringMetric.store_id.is_(None))

        statement = (
            select(MonitoringMetric)
            .where(*filters)
            .order_by(MonitoringMetric.evaluation_date.desc())
            .limit(500)
        )
        newest: Dict[str, MonitoringMetric] = {}
        for metric in self.session.execute(statement).scalars():
            newest.setdefault(metric.metric_name, metric)
        return newest

    def count_metrics(self) -> int:
        """How many metric rows exist."""
        return int(
            self.session.execute(select(func.count(MonitoringMetric.id))).scalar() or 0
        )

    # -- drift -------------------------------------------------------------

    def upsert_drift(
        self,
        *,
        model_version: str,
        feature_name: str,
        drift_method: str,
        drift_score: float,
        threshold: float,
        status: str,
        evaluation_date: date,
        reference_size: Optional[int] = None,
        current_size: Optional[int] = None,
    ) -> DriftMetric:
        """Record one feature's drift score, replacing any value for that date."""
        existing = self.session.execute(
            select(DriftMetric).where(
                DriftMetric.model_version == model_version,
                DriftMetric.feature_name == feature_name,
                DriftMetric.drift_method == drift_method,
                DriftMetric.evaluation_date == evaluation_date,
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.drift_score = float(drift_score)
            existing.threshold = float(threshold)
            existing.status = status
            existing.reference_size = reference_size
            existing.current_size = current_size
            self.session.flush()
            return existing

        metric = DriftMetric(
            model_version=model_version,
            feature_name=feature_name,
            drift_method=drift_method,
            drift_score=float(drift_score),
            threshold=float(threshold),
            status=status,
            evaluation_date=evaluation_date,
            reference_size=reference_size,
            current_size=current_size,
        )
        self.session.add(metric)
        self.session.flush()
        return metric

    def latest_drift(self, *, model_version: Optional[str] = None) -> List[DriftMetric]:
        """
        The most recent drift verdict per feature.

        One row per feature: the drift table shows current state, not history,
        so older evaluations of the same feature are dropped.
        """
        filters = []
        if model_version:
            filters.append(DriftMetric.model_version == model_version)
        statement = (
            select(DriftMetric)
            .where(*filters)
            .order_by(DriftMetric.evaluation_date.desc(), DriftMetric.feature_name)
            .limit(1000)
        )
        newest: Dict[str, DriftMetric] = {}
        for metric in self.session.execute(statement).scalars():
            newest.setdefault(metric.feature_name, metric)
        return sorted(newest.values(), key=lambda item: item.feature_name)

    def count_drift(self) -> int:
        """How many drift rows exist."""
        return int(
            self.session.execute(select(func.count(DriftMetric.id))).scalar() or 0
        )

    # -- alerts ------------------------------------------------------------

    def upsert_alert(
        self,
        *,
        fingerprint: str,
        alert_type: str,
        severity: str,
        title: str,
        message: str,
        recommended_action: Optional[str] = None,
        model_version: Optional[str] = None,
        store_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Alert, bool]:
        """
        Open an alert, or refresh an existing open one. Returns ``(alert, created)``.

        Deduplication by fingerprint is what keeps a daily DAG from filing the
        same ongoing problem thirty times. An already-resolved alert is *not*
        reused: the condition recurring after a human closed it is new
        information and deserves a new row.
        """
        existing = self.session.execute(
            select(Alert)
            .where(
                Alert.fingerprint == fingerprint,
                Alert.status != AlertStatus.RESOLVED.value,
            )
            .order_by(Alert.created_at.desc())
        ).scalars().first()

        if existing is not None:
            existing.severity = severity
            existing.title = title
            existing.message = message
            existing.recommended_action = recommended_action
            existing.details = dict(details or {})
            existing.occurrence_count = int(existing.occurrence_count or 1) + 1
            existing.last_seen_at = utcnow()
            self.session.flush()
            return existing, False

        alert = Alert(
            fingerprint=fingerprint,
            alert_type=alert_type,
            severity=severity,
            title=title,
            message=message,
            recommended_action=recommended_action,
            model_version=model_version,
            store_id=store_id,
            details=dict(details or {}),
            status=AlertStatus.OPEN.value,
        )
        self.session.add(alert)
        self.session.flush()
        return alert, True

    def get_alert(self, alert_id: uuid.UUID) -> Optional[Alert]:
        """One alert by id."""
        return self.session.get(Alert, alert_id)

    def list_alerts(
        self,
        *,
        statuses: Optional[Sequence[str]] = None,
        severities: Optional[Sequence[str]] = None,
        model_version: Optional[str] = None,
        store_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Alert], int]:
        """A page of alerts, newest first, with the unpaginated total."""
        filters = []
        if statuses:
            filters.append(Alert.status.in_(list(statuses)))
        if severities:
            filters.append(Alert.severity.in_(list(severities)))
        if model_version:
            filters.append(Alert.model_version == model_version)
        if store_id:
            filters.append(Alert.store_id == store_id)

        page = (
            select(Alert)
            .where(*filters)
            .order_by(Alert.created_at.desc())
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
        total_statement = select(func.count(Alert.id)).where(*filters)
        rows = list(self.session.execute(page).scalars())
        total = int(self.session.execute(total_statement).scalar() or 0)
        return rows, total

    def alert_counts_by_severity(self) -> Dict[str, int]:
        """Open-alert counts per severity, for the health card."""
        statement = (
            select(Alert.severity, func.count(Alert.id))
            .where(Alert.status != AlertStatus.RESOLVED.value)
            .group_by(Alert.severity)
        )
        return {row[0]: int(row[1]) for row in self.session.execute(statement)}

    def acknowledge_alert(
        self, alert: Alert, *, user_id: Optional[uuid.UUID]
    ) -> Alert:
        """Mark an alert acknowledged. Idempotent."""
        if alert.status == AlertStatus.OPEN.value:
            alert.status = AlertStatus.ACKNOWLEDGED.value
            alert.acknowledged_at = utcnow()
            alert.acknowledged_by_id = user_id
            self.session.flush()
        return alert

    def resolve_alert(
        self, alert: Alert, *, user_id: Optional[uuid.UUID], note: Optional[str] = None
    ) -> Alert:
        """Mark an alert resolved. Idempotent."""
        if alert.status != AlertStatus.RESOLVED.value:
            alert.status = AlertStatus.RESOLVED.value
            alert.resolved_at = utcnow()
            alert.resolved_by_id = user_id
            alert.resolution_note = note
            self.session.flush()
        return alert

    def auto_resolve_stale(self, *, older_than_days: int = 30) -> int:
        """
        Resolve open alerts whose condition has not recurred.

        Without this, an acknowledged alert for a problem that has since gone
        away stays on the dashboard forever and trains people to ignore it.
        """
        cutoff = utcnow() - timedelta(days=max(1, older_than_days))
        statement = select(Alert).where(
            Alert.status != AlertStatus.RESOLVED.value, Alert.last_seen_at < cutoff
        )
        rows = list(self.session.execute(statement).scalars())
        for alert in rows:
            alert.status = AlertStatus.RESOLVED.value
            alert.resolved_at = utcnow()
            alert.resolution_note = (
                f"Auto-resolved: the condition has not recurred for "
                f"{older_than_days} days."
            )
        self.session.flush()
        return len(rows)


def _optional_float(value: Any) -> Optional[float]:
    """Coerce to float, or None for a missing value."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ActualRepository",
    "ForecastRepository",
    "MonitoringRepository",
]
