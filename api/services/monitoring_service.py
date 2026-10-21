"""
Monitoring reads for the dashboard, and the evaluation the DAG runs.

The hard rule in this module: **never invent a number**. Every method
distinguishes "the value is X" from "there is nothing to compute X from", and
the second is returned as an explicit no-data state. A monitoring dashboard
that shows a plausible-looking zero when no forecast has ever been scored is
worse than one that shows nothing, because the zero is believed.

Read paths (used by the API) and the evaluation path (used by the monitoring
DAG) live together because they share the window arithmetic and the threshold
definitions. Splitting them would mean two places to change a threshold.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from api.core.errors import AlertNotFoundError
from api.db.base import as_utc, utcnow
from api.db.models.monitoring import (
    Alert,
    AlertSeverity,
    AlertStatus,
    AlertType,
    DriftStatus,
    HealthStatus,
)
from api.db.repositories.monitoring_repository import (
    ActualRepository,
    ForecastRepository,
    MonitoringRepository,
)
from api.services.monitoring_metrics import (
    ACCURACY_METRIC_NAMES,
    METRIC_BIAS,
    METRIC_COVERAGE,
    METRIC_MAE,
    METRIC_RMSE,
    METRIC_SAMPLE_SIZE,
    METRIC_WAPE,
    AccuracyResult,
    compute_accuracy,
    relative_change,
)

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 28

#: Thresholds that turn a metric into an alert. Env-overridable because what
#: counts as "bad" is a property of the business, not of the code.
WAPE_WARNING = 0.20
WAPE_CRITICAL = 0.35
BIAS_WARNING_FRACTION = 0.10
COVERAGE_WARNING_SHORTFALL = 0.15
STALE_DATA_WARNING_DAYS = 3
STALE_DATA_CRITICAL_DAYS = 7
ACCURACY_REGRESSION_WARNING = 0.25


@dataclass
class WindowAccuracy:
    """Accuracy over a window, plus the change from the window before it."""

    window_start: date
    window_end: date
    result: AccuracyResult
    previous: Optional[AccuracyResult] = None

    @property
    def has_data(self) -> bool:
        """Whether anything was scorable in this window."""
        return not self.result.is_empty

    def trend(self, metric: str) -> Optional[float]:
        """
        Fractional change in one metric against the previous window.

        Positive means the metric got larger. For an error metric that is a
        regression; the UI decides how to colour it, because "larger is worse"
        is true of WAPE and false of coverage.
        """
        if self.previous is None:
            return None
        return relative_change(
            self.result.as_metric_map().get(metric),
            self.previous.as_metric_map().get(metric),
        )


@dataclass
class FreshnessReport:
    """How up to date the monitoring inputs are."""

    latest_actual_date: Optional[date] = None
    latest_forecast_date: Optional[date] = None
    latest_forecast_created_at: Optional[datetime] = None
    days_since_actual: Optional[int] = None
    actual_count: int = 0
    forecast_count: int = 0
    coverage: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        """Whether any actual has been recorded."""
        return self.actual_count > 0

    @property
    def status(self) -> str:
        """Freshness as a status, treating absence as its own state."""
        if not self.has_data:
            return HealthStatus.NO_DATA.value
        if self.days_since_actual is None:
            return HealthStatus.NO_DATA.value
        if self.days_since_actual >= STALE_DATA_CRITICAL_DAYS:
            return HealthStatus.DEGRADED.value
        if self.days_since_actual >= STALE_DATA_WARNING_DAYS:
            return HealthStatus.WARNING.value
        return HealthStatus.HEALTHY.value


@dataclass
class RetrainingRecommendation:
    """What to do about the current Production model."""

    recommendation: str
    reason: str
    model_version: Optional[str] = None
    last_training_date: Optional[str] = None
    wape: Optional[float] = None
    wape_trend: Optional[float] = None
    drift_features: int = 0

    MONITOR = "monitor"
    INVESTIGATE = "investigate"
    RETRAIN = "retrain"
    NO_DATA = "no_data"


class MonitoringService:
    """Monitoring reads and the DAG's evaluation pass."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.forecasts = ForecastRepository(session)
        self.actuals = ActualRepository(session)
        self.monitoring = MonitoringRepository(session)

    # -- freshness ---------------------------------------------------------

    def freshness(self, *, reference_date: Optional[date] = None) -> FreshnessReport:
        """How current the actuals and forecasts are."""
        today = reference_date or utcnow().date()
        latest_actual = self.actuals.latest_date()
        report = FreshnessReport(
            latest_actual_date=latest_actual,
            latest_forecast_date=self.forecasts.latest_forecast_date(),
            latest_forecast_created_at=self.forecasts.latest_forecast_created_at(),
            actual_count=self.actuals.count(),
            forecast_count=self.forecasts.count_forecasts(),
            coverage=self.actuals.coverage_by_store(),
        )
        if latest_actual is not None:
            report.days_since_actual = max(0, (today - latest_actual).days)
        return report

    # -- accuracy ----------------------------------------------------------

    def accuracy(
        self,
        *,
        window_days: int = DEFAULT_WINDOW_DAYS,
        model_version: Optional[str] = None,
        store_id: Optional[str] = None,
        reference_date: Optional[date] = None,
        include_previous: bool = True,
    ) -> WindowAccuracy:
        """
        Score the most recent window, and optionally the one before it.

        The comparison window is the same length immediately before, so a trend
        reflects a change in the model rather than a change in how much data
        happened to be available.
        """
        today = reference_date or utcnow().date()
        span = max(1, window_days)
        window_end = today
        window_start = today - timedelta(days=span - 1)

        pairs = self.forecasts.scored_pairs(
            window_start=window_start,
            window_end=window_end,
            model_version=model_version,
            store_id=store_id,
        )
        current = compute_accuracy(pairs)

        previous: Optional[AccuracyResult] = None
        if include_previous:
            previous_end = window_start - timedelta(days=1)
            previous_start = previous_end - timedelta(days=span - 1)
            previous_pairs = self.forecasts.scored_pairs(
                window_start=previous_start,
                window_end=previous_end,
                model_version=model_version,
                store_id=store_id,
            )
            candidate = compute_accuracy(previous_pairs)
            previous = None if candidate.is_empty else candidate

        return WindowAccuracy(
            window_start=window_start,
            window_end=window_end,
            result=current,
            previous=previous,
        )

    def accuracy_trend(
        self,
        *,
        metric_names: Sequence[str] = (METRIC_WAPE, METRIC_BIAS, METRIC_MAE),
        model_version: Optional[str] = None,
        store_id: Optional[str] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        """
        Stored metric history, as one row per evaluation date.

        Read from ``monitoring_metrics`` rather than recomputed: the DAG's
        recorded value for a date is the record of what was true then, and
        recomputing it today would silently change history as late actuals
        arrive.
        """
        rows = self.monitoring.metric_series(
            metric_names=list(metric_names),
            model_version=model_version,
            store_id=store_id,
            start=start,
            end=end,
        )
        by_date: Dict[date, Dict[str, Any]] = {}
        for metric in rows:
            bucket = by_date.setdefault(
                metric.evaluation_date,
                {
                    "evaluation_date": metric.evaluation_date,
                    "model_version": metric.model_version,
                    "sample_size": metric.sample_size,
                },
            )
            bucket[metric.metric_name] = metric.metric_value
            if metric.sample_size is not None:
                bucket["sample_size"] = metric.sample_size
        return [by_date[key] for key in sorted(by_date)]

    # -- drift -------------------------------------------------------------

    def drift(self, *, model_version: Optional[str] = None) -> List[Dict[str, Any]]:
        """The latest drift verdict per feature."""
        return [
            {
                "feature_name": row.feature_name,
                "drift_method": row.drift_method,
                "drift_score": row.drift_score,
                "threshold": row.threshold,
                "status": row.status,
                "model_version": row.model_version,
                "reference_size": row.reference_size,
                "current_size": row.current_size,
                "evaluation_date": row.evaluation_date,
            }
            for row in self.monitoring.latest_drift(model_version=model_version)
        ]

    # -- alerts ------------------------------------------------------------

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
        """A page of alerts."""
        return self.monitoring.list_alerts(
            statuses=statuses,
            severities=severities,
            model_version=model_version,
            store_id=store_id,
            limit=limit,
            offset=offset,
        )

    def require_alert(self, alert_id: Any) -> Alert:
        """One alert, or a 404."""
        import uuid as _uuid

        try:
            identifier = alert_id if isinstance(alert_id, _uuid.UUID) else _uuid.UUID(
                str(alert_id)
            )
        except (ValueError, AttributeError, TypeError) as exc:
            raise AlertNotFoundError(field="alert_id") from exc

        alert = self.monitoring.get_alert(identifier)
        if alert is None:
            raise AlertNotFoundError(field="alert_id")
        return alert

    # -- summary -----------------------------------------------------------

    def summary(
        self,
        *,
        model_version: Optional[str] = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
        production_model: Optional[Dict[str, Any]] = None,
        reference_date: Optional[date] = None,
    ) -> Dict[str, Any]:
        """
        Everything the dashboard's top half needs, in one read.

        The overall verdict is the worst of the parts, with ``no_data``
        propagating: a system that has never scored a forecast is not healthy,
        and claiming otherwise is the one failure mode a monitoring page cannot
        afford.
        """
        freshness = self.freshness(reference_date=reference_date)
        accuracy = self.accuracy(
            window_days=window_days,
            model_version=model_version,
            reference_date=reference_date,
        )
        drift_rows = self.drift(model_version=model_version)
        alert_counts = self.monitoring.alert_counts_by_severity()

        drifting = [
            row for row in drift_rows if row["status"] == DriftStatus.DRIFT_DETECTED.value
        ]
        warning_drift = [
            row for row in drift_rows if row["status"] == DriftStatus.WARNING.value
        ]

        status = self._overall_status(
            freshness=freshness,
            accuracy=accuracy,
            drifting=len(drifting),
            warning_drift=len(warning_drift),
            alert_counts=alert_counts,
        )

        return {
            "status": status,
            "has_data": accuracy.has_data or freshness.has_data,
            "window_days": window_days,
            "window_start": accuracy.window_start,
            "window_end": accuracy.window_end,
            "model_version": model_version,
            "freshness": freshness,
            "accuracy": accuracy,
            "drift": {
                "evaluated_features": len(drift_rows),
                "drifting_features": len(drifting),
                "warning_features": len(warning_drift),
            },
            "alerts": {
                "open": sum(alert_counts.values()),
                "critical": alert_counts.get(AlertSeverity.CRITICAL.value, 0),
                "warning": alert_counts.get(AlertSeverity.WARNING.value, 0),
                "info": alert_counts.get(AlertSeverity.INFO.value, 0),
            },
            "retraining": self.retraining_recommendation(
                accuracy=accuracy,
                drift_rows=drift_rows,
                production_model=production_model,
            ),
            "no_data_message": (
                None
                if (accuracy.has_data or freshness.has_data)
                else NO_DATA_MESSAGE
            ),
        }

    @staticmethod
    def _overall_status(
        *,
        freshness: FreshnessReport,
        accuracy: WindowAccuracy,
        drifting: int,
        warning_drift: int,
        alert_counts: Dict[str, int],
    ) -> str:
        """Fold the parts into one verdict, worst-wins."""
        if not freshness.has_data and not accuracy.has_data:
            return HealthStatus.NO_DATA.value

        if alert_counts.get(AlertSeverity.CRITICAL.value, 0) > 0:
            return HealthStatus.DEGRADED.value

        wape = accuracy.result.wape
        if wape is not None and wape >= WAPE_CRITICAL:
            return HealthStatus.DEGRADED.value
        if freshness.status == HealthStatus.DEGRADED.value:
            return HealthStatus.DEGRADED.value
        if drifting > 0:
            return HealthStatus.DEGRADED.value

        if (
            (wape is not None and wape >= WAPE_WARNING)
            or warning_drift > 0
            or freshness.status == HealthStatus.WARNING.value
            or alert_counts.get(AlertSeverity.WARNING.value, 0) > 0
        ):
            return HealthStatus.WARNING.value

        if not accuracy.has_data:
            # Fresh inputs but nothing scored yet: honest state, not health.
            return HealthStatus.NO_DATA.value

        return HealthStatus.HEALTHY.value

    def retraining_recommendation(
        self,
        *,
        accuracy: WindowAccuracy,
        drift_rows: Sequence[Dict[str, Any]],
        production_model: Optional[Dict[str, Any]] = None,
    ) -> RetrainingRecommendation:
        """
        Recommend monitor / investigate / retrain, with the reason.

        A recommendation without a reason is an instruction, and nobody should
        retrain a model because a dashboard said so without knowing why.
        """
        version = (production_model or {}).get("version")
        trained = (production_model or {}).get("training_date")
        drifting = sum(
            1 for row in drift_rows if row["status"] == DriftStatus.DRIFT_DETECTED.value
        )
        wape = accuracy.result.wape
        wape_trend = accuracy.trend(METRIC_WAPE)

        if not accuracy.has_data:
            # Drift is evaluated from stored rows and can be known even when the
            # current window has nothing scorable. Reporting a flat "no data"
            # while the drift table shows detected drift would have the summary
            # contradicting the panel below it.
            if drifting:
                return RetrainingRecommendation(
                    recommendation=RetrainingRecommendation.INVESTIGATE,
                    reason=(
                        f"{drifting} feature(s) have drifted beyond their "
                        "threshold. Accuracy cannot be measured for the current "
                        "window, so the drift signal is all there is to go on."
                    ),
                    model_version=version,
                    last_training_date=trained,
                    drift_features=drifting,
                )
            return RetrainingRecommendation(
                recommendation=RetrainingRecommendation.NO_DATA,
                reason=(
                    "No forecast has been scored against an actual in this "
                    "window, so model performance cannot be assessed."
                ),
                model_version=version,
                last_training_date=trained,
                drift_features=drifting,
            )

        if (wape is not None and wape >= WAPE_CRITICAL) or drifting >= 3:
            reason = (
                f"Forecast error is {wape:.1%} over the window."
                if wape is not None and wape >= WAPE_CRITICAL
                else f"{drifting} features have drifted beyond their threshold."
            )
            return RetrainingRecommendation(
                recommendation=RetrainingRecommendation.RETRAIN,
                reason=reason,
                model_version=version,
                last_training_date=trained,
                wape=wape,
                wape_trend=wape_trend,
                drift_features=drifting,
            )

        if (
            (wape is not None and wape >= WAPE_WARNING)
            or drifting > 0
            or (wape_trend is not None and wape_trend >= ACCURACY_REGRESSION_WARNING)
        ):
            if wape_trend is not None and wape_trend >= ACCURACY_REGRESSION_WARNING:
                reason = (
                    f"Error rose {wape_trend:.0%} against the previous window."
                )
            elif drifting > 0:
                reason = f"{drifting} feature(s) have drifted beyond their threshold."
            else:
                reason = f"Forecast error is {wape:.1%} over the window."
            return RetrainingRecommendation(
                recommendation=RetrainingRecommendation.INVESTIGATE,
                reason=reason,
                model_version=version,
                last_training_date=trained,
                wape=wape,
                wape_trend=wape_trend,
                drift_features=drifting,
            )

        return RetrainingRecommendation(
            recommendation=RetrainingRecommendation.MONITOR,
            reason=(
                f"Forecast error is {wape:.1%} and no feature has drifted."
                if wape is not None
                else "No threshold has been crossed."
            ),
            model_version=version,
            last_training_date=trained,
            wape=wape,
            wape_trend=wape_trend,
            drift_features=drifting,
        )

    # -- evaluation (used by the monitoring DAG) ---------------------------

    def persist_accuracy(
        self,
        *,
        model_version: str,
        accuracy: WindowAccuracy,
        evaluation_date: date,
        model_name: Optional[str] = None,
        store_id: Optional[str] = None,
    ) -> int:
        """
        Store a window's metrics, returning how many rows were written.

        Nothing is written for an empty window: a row of zeros would later be
        indistinguishable from a genuinely perfect window.
        """
        if accuracy.result.is_empty:
            logger.info(
                "Nothing scorable for %s in %s..%s; no metrics written",
                model_version,
                accuracy.window_start,
                accuracy.window_end,
            )
            return 0

        written = 0
        metrics = accuracy.result.as_metric_map()
        metrics[METRIC_SAMPLE_SIZE] = float(accuracy.result.sample_size)

        for name, value in metrics.items():
            self.monitoring.upsert_metric(
                model_version=model_version,
                model_name=model_name,
                store_id=store_id,
                metric_name=name,
                metric_value=value,
                sample_size=accuracy.result.sample_size,
                evaluation_date=evaluation_date,
                window_start=accuracy.window_start,
                window_end=accuracy.window_end,
            )
            written += 1
        return written

    def evaluate_alerts(
        self,
        *,
        model_version: str,
        accuracy: WindowAccuracy,
        freshness: FreshnessReport,
        drift_rows: Sequence[Dict[str, Any]],
        confidence_level: float = 0.95,
    ) -> List[Tuple[Alert, bool]]:
        """
        Apply the alert thresholds, opening or refreshing alerts.

        Returns ``(alert, created)`` per condition. Each alert carries a
        recommended action: an alert that only states a number leaves the
        reader to work out what to do, which is how alerts get ignored.
        """
        raised: List[Tuple[Alert, bool]] = []

        wape = accuracy.result.wape
        if wape is not None:
            if wape >= WAPE_CRITICAL:
                raised.append(
                    self._raise(
                        alert_type=AlertType.ACCURACY_DEGRADATION,
                        severity=AlertSeverity.CRITICAL,
                        model_version=model_version,
                        title=f"Forecast error is {wape:.1%}",
                        message=(
                            f"WAPE over {accuracy.window_start} to "
                            f"{accuracy.window_end} is {wape:.1%}, above the "
                            f"{WAPE_CRITICAL:.0%} critical threshold, across "
                            f"{accuracy.result.sample_size} scored store-days."
                        ),
                        recommended_action=(
                            "Retrain on recent data and compare the candidate "
                            "against the current Production version before "
                            "promoting it."
                        ),
                        details={
                            "wape": wape,
                            "threshold": WAPE_CRITICAL,
                            "sample_size": accuracy.result.sample_size,
                        },
                    )
                )
            elif wape >= WAPE_WARNING:
                raised.append(
                    self._raise(
                        alert_type=AlertType.ACCURACY_DEGRADATION,
                        severity=AlertSeverity.WARNING,
                        model_version=model_version,
                        title=f"Forecast error is {wape:.1%}",
                        message=(
                            f"WAPE over {accuracy.window_start} to "
                            f"{accuracy.window_end} is {wape:.1%}, above the "
                            f"{WAPE_WARNING:.0%} warning threshold."
                        ),
                        recommended_action=(
                            "Review which stores contribute most of the error "
                            "before deciding whether to retrain."
                        ),
                        details={"wape": wape, "threshold": WAPE_WARNING},
                    )
                )

        # Bias is judged relative to the typical error, not in absolute units:
        # a bias of 50 is trivial on a mean of 3,000 and severe on a mean of 60.
        bias = accuracy.result.bias
        mae = accuracy.result.mae
        if bias is not None and mae is not None and mae > 0:
            bias_fraction = abs(bias) / mae
            if bias_fraction >= 1.0 - BIAS_WARNING_FRACTION:
                direction = "over" if bias > 0 else "under"
                raised.append(
                    self._raise(
                        alert_type=AlertType.BIAS,
                        severity=AlertSeverity.WARNING,
                        model_version=model_version,
                        title=f"Forecasts are consistently {direction} actuals",
                        message=(
                            f"Mean error is {bias:,.1f} against a mean absolute "
                            f"error of {mae:,.1f}, so nearly all of the error is "
                            f"in one direction."
                        ),
                        recommended_action=(
                            "A one-directional error is usually correctable by "
                            "recalibration rather than a full retrain."
                        ),
                        details={"bias": bias, "mae": mae},
                    )
                )

        coverage = accuracy.result.interval_coverage
        if coverage is not None and coverage < confidence_level - COVERAGE_WARNING_SHORTFALL:
            raised.append(
                self._raise(
                    alert_type=AlertType.INTERVAL_COVERAGE,
                    severity=AlertSeverity.WARNING,
                    model_version=model_version,
                    title=f"Interval coverage is {coverage:.0%}",
                    message=(
                        f"The {confidence_level:.0%} interval contained the actual "
                        f"{coverage:.0%} of the time, so it is narrower than it "
                        f"claims."
                    ),
                    recommended_action=(
                        "The uncertainty band is understated. Widen the residual "
                        "estimate or treat the published interval as optimistic."
                    ),
                    details={"coverage": coverage, "confidence_level": confidence_level},
                )
            )

        if freshness.days_since_actual is not None:
            days = freshness.days_since_actual
            if days >= STALE_DATA_CRITICAL_DAYS:
                raised.append(
                    self._raise(
                        alert_type=AlertType.STALE_DATA,
                        severity=AlertSeverity.CRITICAL,
                        model_version=model_version,
                        title=f"No actuals for {days} days",
                        message=(
                            f"The most recent actual is {freshness.latest_actual_date}. "
                            "Accuracy cannot be measured without outcomes."
                        ),
                        recommended_action=(
                            "Check the ingestion that loads actuals. Every "
                            "monitoring figure is stale until it resumes."
                        ),
                        details={"days_since_actual": days},
                    )
                )
            elif days >= STALE_DATA_WARNING_DAYS:
                raised.append(
                    self._raise(
                        alert_type=AlertType.STALE_DATA,
                        severity=AlertSeverity.WARNING,
                        model_version=model_version,
                        title=f"Actuals are {days} days behind",
                        message=(
                            f"The most recent actual is {freshness.latest_actual_date}."
                        ),
                        recommended_action="Confirm the actuals ingestion is running.",
                        details={"days_since_actual": days},
                    )
                )

        drifted = [
            row
            for row in drift_rows
            if row["status"] == DriftStatus.DRIFT_DETECTED.value
        ]
        if drifted:
            names = ", ".join(sorted(row["feature_name"] for row in drifted)[:8])
            raised.append(
                self._raise(
                    alert_type=AlertType.FEATURE_DRIFT,
                    severity=(
                        AlertSeverity.CRITICAL
                        if len(drifted) >= 3
                        else AlertSeverity.WARNING
                    ),
                    model_version=model_version,
                    title=f"{len(drifted)} feature(s) have drifted",
                    message=(
                        f"Beyond threshold: {names}. The live input distribution "
                        "no longer matches what the model was trained on."
                    ),
                    recommended_action=(
                        "Retrain on recent data. Drifted inputs make the "
                        "published accuracy figures optimistic."
                    ),
                    details={
                        "features": sorted(row["feature_name"] for row in drifted),
                        "count": len(drifted),
                    },
                )
            )

        return raised

    def _raise(
        self,
        *,
        alert_type: AlertType,
        severity: AlertSeverity,
        model_version: Optional[str],
        title: str,
        message: str,
        recommended_action: str,
        details: Dict[str, Any],
        store_id: Optional[str] = None,
    ) -> Tuple[Alert, bool]:
        """Open or refresh one alert, keyed on a stable fingerprint."""
        return self.monitoring.upsert_alert(
            fingerprint=alert_fingerprint(
                alert_type=alert_type.value,
                model_version=model_version,
                store_id=store_id,
            ),
            alert_type=alert_type.value,
            severity=severity.value,
            title=title,
            message=message,
            recommended_action=recommended_action,
            model_version=model_version,
            store_id=store_id,
            details=details,
        )


NO_DATA_MESSAGE = (
    "Monitoring will populate after forecasts and actual outcomes are recorded."
)


def alert_fingerprint(
    *, alert_type: str, model_version: Optional[str], store_id: Optional[str] = None
) -> str:
    """
    Stable identity for an alert condition.

    Deliberately excludes the measured value: an accuracy problem that worsens
    from 22% to 24% is the same ongoing condition, and a value-sensitive
    fingerprint would file it as a new alert every day.
    """
    raw = f"{alert_type}|{model_version or '-'}|{store_id or '-'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def monitoring_window_days() -> int:
    """Evaluation window length, from the environment."""
    try:
        value = int(os.getenv("MONITORING_WINDOW_DAYS", str(DEFAULT_WINDOW_DAYS)))
    except ValueError:
        return DEFAULT_WINDOW_DAYS
    return max(1, min(value, 365))


__all__ = [
    "ACCURACY_REGRESSION_WARNING",
    "DEFAULT_WINDOW_DAYS",
    "NO_DATA_MESSAGE",
    "STALE_DATA_CRITICAL_DAYS",
    "STALE_DATA_WARNING_DAYS",
    "WAPE_CRITICAL",
    "WAPE_WARNING",
    "FreshnessReport",
    "MonitoringService",
    "RetrainingRecommendation",
    "WindowAccuracy",
    "alert_fingerprint",
    "monitoring_window_days",
]
