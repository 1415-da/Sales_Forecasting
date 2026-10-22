"""
The monitoring evaluation pass.

One function the scheduler triggers: load recent actuals, score forecasts
against them, measure drift, raise alerts, and report what happened.

Why this is in the API and not the DAG
--------------------------------------
Airflow 3 pins SQLAlchemy 1.4; the API's models use the 2.0 declarative API, so
a DAG task that imported ``api.db`` would fail at import time. Rather than
maintain a second, 1.4-compatible copy of the schema and the metrics, the
evaluation runs here and the DAG calls it over HTTP. One implementation, one
dependency set, and the DAG stays a scheduler rather than a second home for
business logic.

Drift here is **prediction** drift, not feature drift: the live feature matrix
is not persisted, so there is nothing to compare input distributions against.
The forecast-error distribution *is* available, and shifting error answers a
closely related question -- is the model behaving differently than it was. The
response says which one was measured rather than letting the label imply the
other.
"""

from __future__ import annotations

import glob
import logging
import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from api.db.repositories.monitoring_repository import (
    ActualRepository,
    ForecastRepository,
    MonitoringRepository,
)
from api.schemas.monitoring_evaluate import (
    ActualsLoadResult,
    EvaluateRequest,
    EvaluateResponse,
)
from api.services.model_registry_service import ModelRegistryService
from api.services.monitoring_metrics import (
    MIN_DRIFT_SAMPLE,
    DriftMethod,
    evaluate_drift,
)
from api.services.monitoring_service import (
    NO_DATA_MESSAGE,
    MonitoringService,
)

logger = logging.getLogger(__name__)

#: Default location of the partitioned parquet the training pipeline writes.
DEFAULT_ACTUALS_PATH = "/usr/local/airflow/data/raw/kaggle_m5/v1.0.0/sales"

UNKNOWN_VERSION = "unknown"


def actuals_path() -> str:
    """Where observed sales are read from."""
    return os.getenv("MONITORING_ACTUALS_PATH", DEFAULT_ACTUALS_PATH)


class MonitoringEvaluator:
    """Runs one evaluation pass over one database session."""

    def __init__(
        self,
        session: Session,
        registry: Optional[ModelRegistryService] = None,
    ) -> None:
        self.session = session
        self.monitoring = MonitoringService(session)
        self.actuals = ActualRepository(session)
        self.forecasts = ForecastRepository(session)
        self.store = MonitoringRepository(session)
        self._registry = registry

    @property
    def registry(self) -> ModelRegistryService:
        """Model registry access, built on first use."""
        if self._registry is None:
            self._registry = ModelRegistryService()
        return self._registry

    def run(self, request: EvaluateRequest) -> EvaluateResponse:
        """Execute the full pass and report it."""
        actuals = (
            ActualsLoadResult(skipped_reason="skipped by request")
            if request.skip_actuals
            else self.load_actuals(request.actuals_lookback_days)
        )
        # Resolved after loading, so a data-anchored clock sees today's load.
        today = self.monitoring.reference_date(request.reference_date)

        version, model_name, production = self._resolve_version(request.model_version)

        accuracy = self.monitoring.accuracy(
            window_days=request.window_days,
            model_version=None if version == UNKNOWN_VERSION else version,
            reference_date=today,
        )
        metrics_written = self.monitoring.persist_accuracy(
            model_version=version,
            model_name=model_name,
            accuracy=accuracy,
            evaluation_date=today,
        )

        drift_evaluated, drift_detected, drift_reason = self.evaluate_drift(
            model_version=version,
            window_days=request.window_days,
            reference_date=today,
        )

        freshness = self.monitoring.freshness(reference_date=today)
        drift_rows = self.monitoring.drift(
            model_version=None if version == UNKNOWN_VERSION else version
        )
        raised = self.monitoring.evaluate_alerts(
            model_version=version,
            accuracy=accuracy,
            freshness=freshness,
            drift_rows=drift_rows,
        )
        auto_resolved = self.store.auto_resolve_stale(
            older_than_days=request.alert_stale_days
        )

        recommendation = self.monitoring.retraining_recommendation(
            accuracy=accuracy,
            drift_rows=drift_rows,
            production_model=production,
        )
        summary = self.monitoring.summary(
            model_version=None if version == UNKNOWN_VERSION else version,
            window_days=request.window_days,
            production_model=production,
            reference_date=today,
        )

        return EvaluateResponse(
            status=summary["status"],
            model_version=version,
            window_start=accuracy.window_start,
            window_end=accuracy.window_end,
            actuals=actuals,
            scored_pairs=accuracy.result.sample_size,
            metrics_written=metrics_written,
            metrics={
                name: round(value, 6)
                for name, value in accuracy.result.as_metric_map().items()
            },
            drift_evaluated=drift_evaluated,
            drift_detected=drift_detected,
            drift_skipped_reason=drift_reason,
            alerts_created=sum(1 for _, is_new in raised if is_new),
            alerts_refreshed=sum(1 for _, is_new in raised if not is_new),
            alerts_auto_resolved=auto_resolved,
            alert_titles=[alert.title for alert, _ in raised],
            recommendation=recommendation.recommendation,
            recommendation_reason=recommendation.reason,
            no_data_message=summary["no_data_message"],
        )

    # -- steps -------------------------------------------------------------

    def load_actuals(self, lookback_days: int) -> ActualsLoadResult:
        """
        Load recent observed sales from the training pipeline's parquet.

        Aggregated to store-day with the same ``sum`` over ``revenue`` that
        training uses, so "actual sales" means one thing across the platform.
        Upserted rather than inserted: a repeated store-day is a correction,
        and inserting it again would double-count the day in every metric.

        A deployment with a real point-of-sale feed should replace this method
        and leave everything else alone -- all of monitoring depends only on
        the ``actuals`` table.
        """
        root = actuals_path()
        files = sorted(glob.glob(os.path.join(root, "**", "*.parquet"), recursive=True))
        if not files:
            logger.info("No actuals parquet found under %s", root)
            return ActualsLoadResult(
                skipped_reason="no source parquet files were found"
            )

        selected = files[-max(1, lookback_days):]

        try:
            import pandas as pd
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            logger.error("pandas is unavailable, cannot load actuals: %s", exc)
            return ActualsLoadResult(skipped_reason="pandas is unavailable")

        frames = []
        for path in selected:
            try:
                frames.append(pd.read_parquet(path))
            except Exception as exc:  # noqa: BLE001 - skip one bad file
                logger.warning("Skipping unreadable parquet %s: %s", path, exc)

        if not frames:
            return ActualsLoadResult(
                files_read=0, skipped_reason="no readable parquet files"
            )

        combined = pd.concat(frames, ignore_index=True)
        missing = [
            column
            for column in ("date", "store_id", "revenue")
            if column not in combined.columns
        ]
        if missing:
            logger.error(
                "Actuals parquet lacks %s; cannot derive store-day actuals", missing
            )
            return ActualsLoadResult(
                files_read=len(frames),
                skipped_reason=f"source is missing columns: {', '.join(missing)}",
            )

        combined["date"] = pd.to_datetime(combined["date"]).dt.date
        store_day = (
            combined.groupby(["date", "store_id"], as_index=False)["revenue"]
            .sum()
            .rename(columns={"revenue": "actual_sales"})
        )

        rows = [
            {
                "store_id": str(record["store_id"]),
                "date": record["date"],
                "actual_sales": float(record["actual_sales"]),
            }
            for record in store_day.to_dict("records")
        ]
        inserted, updated = self.actuals.upsert_many(
            rows, source="training_dataset"
        )

        logger.info(
            "Actuals: %d inserted, %d corrected, from %d file(s)",
            inserted,
            updated,
            len(frames),
        )
        return ActualsLoadResult(
            loaded=len(rows),
            inserted=inserted,
            updated=updated,
            files_read=len(frames),
            date_min=min(row["date"] for row in rows) if rows else None,
            date_max=max(row["date"] for row in rows) if rows else None,
        )

    def evaluate_drift(
        self, *, model_version: str, window_days: int, reference_date: date
    ) -> tuple[int, int, Optional[str]]:
        """
        Measure prediction drift over two adjacent windows.

        Returns ``(evaluated, drifting, skipped_reason)``. A window with fewer
        than ``MIN_DRIFT_SAMPLE`` scored days is skipped rather than scored: a
        PSI from a dozen points is noise, and an alert raised on it would teach
        people to ignore drift alerts.
        """
        span = max(1, window_days)
        current_start = reference_date - timedelta(days=span - 1)
        reference_end = current_start - timedelta(days=1)
        reference_start = reference_end - timedelta(days=span - 1)
        version_filter = None if model_version == UNKNOWN_VERSION else model_version

        current = self.forecasts.scored_pairs(
            window_start=current_start,
            window_end=reference_date,
            model_version=version_filter,
        )
        reference = self.forecasts.scored_pairs(
            window_start=reference_start,
            window_end=reference_end,
            model_version=version_filter,
        )

        if len(current) < MIN_DRIFT_SAMPLE or len(reference) < MIN_DRIFT_SAMPLE:
            reason = (
                f"needs {MIN_DRIFT_SAMPLE} scored days in each window; have "
                f"{len(reference)} and {len(current)}"
            )
            logger.info("Skipping drift evaluation: %s", reason)
            return 0, 0, reason

        series = {
            # Relative error, so the comparison is not dominated by whichever
            # window happened to contain the higher-volume stores.
            "forecast_error": (
                _relative_errors(reference),
                _relative_errors(current),
            ),
            "predicted_sales": (
                [item["predicted"] for item in reference],
                [item["predicted"] for item in current],
            ),
            "actual_sales": (
                [item["actual"] for item in reference],
                [item["actual"] for item in current],
            ),
        }

        evaluated = 0
        detected = 0
        for name, (reference_values, current_values) in series.items():
            result = evaluate_drift(
                feature_name=name,
                reference=reference_values,
                current=current_values,
                method=DriftMethod.PSI,
            )
            self.store.upsert_drift(
                model_version=model_version,
                feature_name=result.feature_name,
                drift_method=result.method.value,
                drift_score=result.score,
                threshold=result.threshold,
                status=result.status,
                evaluation_date=reference_date,
                reference_size=result.reference_size,
                current_size=result.current_size,
            )
            evaluated += 1
            if result.status == "drift_detected":
                detected += 1

        return evaluated, detected, None

    def _resolve_version(
        self, requested: Optional[str]
    ) -> tuple[str, Optional[str], Optional[Dict[str, Any]]]:
        """
        Identify the version being scored.

        Metrics are stored per version, so without this the history would mix
        versions and a promotion would look like a sudden accuracy change.
        """
        production: Optional[Dict[str, Any]] = None
        try:
            production = self.registry.production_summary()
        except Exception as exc:  # noqa: BLE001 - the registry is not essential
            logger.info("Could not read the production model: %s", exc)

        if requested:
            return requested, (production or {}).get("model"), production
        if production:
            return production["version"], production.get("model"), production
        return UNKNOWN_VERSION, None, None


def _relative_errors(pairs: Sequence[Dict[str, Any]]) -> List[float]:
    """Per-day relative forecast error, skipping zero-actual days."""
    return [
        (item["predicted"] - item["actual"]) / item["actual"]
        for item in pairs
        if item["actual"]
    ]


__all__ = [
    "DEFAULT_ACTUALS_PATH",
    "NO_DATA_MESSAGE",
    "UNKNOWN_VERSION",
    "MonitoringEvaluator",
    "actuals_path",
]
