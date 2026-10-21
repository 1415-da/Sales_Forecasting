"""
The two adapters that make the Scenario Planner work.

The planner's design splits a comparison into *build a future feature matrix*
and *predict over it*. Those two ports had no implementation, which is why
``POST /v1/scenario/compare`` previously returned ``PREDICTION_FAILED``.

How the split is reconciled with a recursive model
--------------------------------------------------
The trained models are single-step: each day's lag and rolling inputs come from
the days before it. A pure "build the whole matrix, then predict it in one
shot" reading of the ports cannot express that -- the lags for day 5 depend on
the prediction for day 4, which does not exist until prediction runs.

The resolution is a clean division of ownership:

* **The matrix carries exogenous columns.** The override engine is already
  documented never to touch lag or rolling columns, so what a scenario changes
  is precisely the set of columns that do not depend on the model's own output:
  promotion flags, traffic, holiday markers, closure.
* **The predictor owns the autoregressive columns.** It walks the matrix in
  date order and recomputes every lag and rolling feature from its own evolving
  path, seeded from the caller's observed history.

That is what makes a promotion scenario *compound*: a lift on day 1 raises
day 2's lag-1 input, which raises day 2's forecast, and so on. Predicting the
overridden matrix row-by-row without recomputing lags would apply the lift to
each day in isolation and systematically understate a sustained scenario.

The baseline leg goes through ``ForecastService.project``, which is also what
``POST /v1/forecast`` runs, so a baseline here and a forecast there are the
same number.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from api.config import Settings, get_settings
from api.core.errors import PredictionFailedError
from api.schemas.baseline_forecast import UncertaintyMethod
from api.schemas.scenario import HistoryPoint
from api.schemas.scenario_capabilities import FeatureSchema
from api.services.baseline_forecast_service import ResolvedModel
from api.services.feature_service import FeatureBuilder
from api.services.forecast_service import (
    MAX_RESIDUAL_FRACTION,
    MIN_RESIDUAL_FRACTION,
    Z_SCORES,
    ForecastService,
)
from api.services.model_registry_service import ResolvedModelVersion
from api.services.scenario_forecast_service import MatrixPrediction

logger = logging.getLogger(__name__)

#: The column the matrix uses to carry its forecast date. Matches the override
#: engine's ``DEFAULT_DATE_COLUMN`` so the two agree on where the date lives.
DATE_COLUMN = "date"

#: Quantiles the planner reports.
QUANTILE_P10 = 0.10
QUANTILE_P50 = 0.50
QUANTILE_P90 = 0.90

#: Interval half-width multiplier for P10/P90, i.e. an 80% central interval.
_P80_Z = Z_SCORES[0.8]


@dataclass
class _Resolved:
    """A scenario ``ResolvedModel`` projected onto the registry's own type."""

    version: ResolvedModelVersion
    rmse: Optional[float]


class ScenarioMatrixBuilder:
    """
    ``FutureFeatureBuilder``: produces the baseline's future feature matrix.

    Returns exactly the rows the baseline forecast was computed from, with a
    ``date`` column prepended. Returning a freshly derived matrix instead would
    risk the scenario being compared against features the baseline never saw.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        forecast_service: Optional[ForecastService] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._forecast_service = forecast_service

    @property
    def forecast_service(self) -> ForecastService:
        """The shared forecast pipeline, built on first use."""
        if self._forecast_service is None:
            self._forecast_service = ForecastService(self.settings)
        return self._forecast_service

    def build(
        self,
        *,
        history: Sequence[HistoryPoint],
        store_id: str,
        forecast_start_date: date,
        forecast_end_date: date,
        feature_schema: FeatureSchema,
        model: ResolvedModel,
    ) -> Any:
        """
        Build one row per forecast day, with the model's feature columns.

        The horizon is taken from the requested window rather than from the
        schema, and the baseline projection is run over exactly that many days
        so the matrix and the window cannot disagree.
        """
        import pandas as pd

        horizon = (forecast_end_date - forecast_start_date).days + 1
        if horizon < 1:
            raise PredictionFailedError(
                "The scenario window is empty.",
                field="scenario",
                details={
                    "forecast_start_date": forecast_start_date.isoformat(),
                    "forecast_end_date": forecast_end_date.isoformat(),
                },
            )

        projection = self._project(
            history=history, store_id=store_id, horizon=horizon, model=model
        )

        rows: List[Dict[str, Any]] = []
        for offset, row in enumerate(projection.feature_rows):
            record: Dict[str, Any] = {
                DATE_COLUMN: forecast_start_date + timedelta(days=offset)
            }
            record.update(row)
            rows.append(record)

        if not rows:
            raise PredictionFailedError(
                "The baseline produced no feature rows.",
                details={"store_id": store_id, "horizon_days": horizon},
            )

        frame = pd.DataFrame(rows)
        # Cached so the predictor can reuse this run's builder and bundle
        # instead of resolving and loading the model a second time.
        self._last_projection = projection
        return frame

    def _project(
        self,
        *,
        history: Sequence[HistoryPoint],
        store_id: str,
        horizon: int,
        model: ResolvedModel,
    ) -> Any:
        """Run the shared pipeline for the baseline leg."""
        resolved = ResolvedModelVersion(
            model=self.settings.default_model,
            registered_model_name=self.settings.registered_model_name(
                self.settings.default_model
            ),
            version=model.model_version,
            stage=model.model_stage,
            run_id=model.run_id,
        )
        observations = [_ForecastPoint.from_history(point) for point in history]
        return self.forecast_service.project(
            resolved=resolved,
            history=observations,
            store_id=store_id,
            horizon_days=horizon,
        )

    @property
    def last_projection(self) -> Optional[Any]:
        """The most recent baseline projection, for the predictor to reuse."""
        return getattr(self, "_last_projection", None)


class RecursiveMatrixPredictor:
    """
    ``FeatureMatrixPredictor``: predicts a feature matrix recursively.

    Walks the matrix in date order. For each row it keeps the exogenous columns
    exactly as supplied -- those are what the scenario changed -- and recomputes
    every lag and rolling column from the path it has produced so far. That is
    what lets a multi-day scenario compound instead of being applied to each
    day independently.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        builder_source: Optional[ScenarioMatrixBuilder] = None,
        forecast_service: Optional[ForecastService] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.builder_source = builder_source
        self._forecast_service = forecast_service

    @property
    def forecast_service(self) -> ForecastService:
        """The shared forecast pipeline, built on first use."""
        if self._forecast_service is None:
            self._forecast_service = ForecastService(self.settings)
        return self._forecast_service

    def predict_matrix(
        self,
        *,
        features: Any,
        model: ResolvedModel,
        confidence_levels: Sequence[float],
    ) -> MatrixPrediction:
        """
        Predict every row, then attach P10/P50/P90.

        The interval is residual-derived from the model's own held-out RMSE and
        widened by ``sqrt(step)``, matching the forecast endpoint exactly. It is
        reported as ``RESIDUAL_BASED`` rather than as model-native quantiles,
        because that is what it is.
        """
        import pandas as pd

        if features is None or len(features) == 0:
            raise PredictionFailedError(
                "There are no feature rows to predict.", field="scenario"
            )

        projection = (
            self.builder_source.last_projection if self.builder_source else None
        )
        if projection is None or projection.builder is None:
            raise PredictionFailedError(
                "The scenario feature matrix was not produced by this service, "
                "so its model could not be resolved.",
                details={"reason": "no baseline projection in scope"},
            )

        builder: FeatureBuilder = projection.builder
        bundle = projection.bundle
        feature_columns = builder.feature_cols

        ordered = features.sort_values(DATE_COLUMN).reset_index(drop=True)
        dates: List[date] = [_as_date(value) for value in ordered[DATE_COLUMN]]

        # Seed from the caller's observed history, so day 1's lags are real
        # observations rather than anything the scenario invented.
        series: List[float] = [float(value) for value in projection.series.sales]
        predictions: List[float] = []

        for position in range(len(ordered)):
            row = {
                column: float(ordered.at[position, column])
                for column in feature_columns
                if column in ordered.columns
            }

            # Recompute the autoregressive columns from the path produced so
            # far, overwriting whatever the matrix carried for them.
            row.update(builder.lag_features(series))
            row.update(builder.rolling_features(series))

            missing = [column for column in feature_columns if column not in row]
            if missing:
                raise PredictionFailedError(
                    "The scenario feature matrix is missing model inputs.",
                    field="scenario",
                    details={"missing_features": sorted(missing)[:20]},
                )

            frame = builder.to_frame([row])
            try:
                raw = bundle.model.predict(builder.scale(frame))
            except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
                raise PredictionFailedError(
                    "The model failed while producing the scenario forecast.",
                    details={
                        "forecast_day": dates[position].isoformat(),
                        "error": str(exc),
                    },
                ) from exc

            value = _first_scalar(raw)
            if value is None or not math.isfinite(value):
                raise PredictionFailedError(
                    "The model returned a non-numeric scenario prediction.",
                    details={"forecast_day": dates[position].isoformat()},
                )
            value = max(0.0, value)
            predictions.append(value)
            series.append(value)

        quantiles = self._build_quantiles(
            predictions=predictions,
            residual_std=self._residual_std(projection, predictions),
        )
        return MatrixPrediction(
            dates=dates,
            predictions=predictions,
            quantiles=quantiles,
            uncertainty_method=UncertaintyMethod.RESIDUAL_BASED,
        )

    @staticmethod
    def _residual_std(projection: Any, predictions: Sequence[float]) -> float:
        """
        Residual spread for the interval, bounded relative to the level.

        Falls back to the history's own volatility when the training run
        recorded no error metric -- the same fallback the forecast endpoint
        uses, so the two produce comparable bands.
        """
        level = (
            sum(predictions) / len(predictions)
            if predictions
            else sum(projection.series.sales) / max(1, len(projection.series.sales))
        )
        floor = MIN_RESIDUAL_FRACTION * abs(level)
        ceiling = MAX_RESIDUAL_FRACTION * abs(level)

        values = list(projection.series.sales)
        if len(values) >= 2:
            mean = sum(values) / len(values)
            variance = sum((item - mean) ** 2 for item in values) / (len(values) - 1)
            fallback = math.sqrt(variance)
        else:
            fallback = floor

        return min(max(fallback, floor), ceiling)

    @staticmethod
    def _build_quantiles(
        *, predictions: Sequence[float], residual_std: float
    ) -> Dict[float, List[float]]:
        """
        P10/P50/P90 around the point path.

        Widened by ``sqrt(step)`` because a recursive forecast inherits its own
        error, so variance accumulates roughly linearly in the number of steps.
        """
        p10: List[float] = []
        p90: List[float] = []
        for step, value in enumerate(predictions, start=1):
            half_width = _P80_Z * residual_std * math.sqrt(step)
            p10.append(round(max(0.0, value - half_width), 4))
            p90.append(round(value + half_width, 4))
        return {
            QUANTILE_P10: p10,
            QUANTILE_P50: [round(value, 4) for value in predictions],
            QUANTILE_P90: p90,
        }


@dataclass
class _ForecastPoint:
    """
    A scenario history point in the shape the feature builder reads.

    A small adapter rather than the forecast endpoint's pydantic model: the
    builder reads attributes duck-typed, and re-validating through the schema
    would repeat work the scenario layer has already done.
    """

    date: date
    sales: float
    store_id: Optional[str] = None
    quantity_sold: Optional[float] = None
    profit: Optional[float] = None
    customer_traffic: Optional[float] = None
    has_promotion: Optional[bool] = None

    @classmethod
    def from_history(cls, point: HistoryPoint) -> "_ForecastPoint":
        """Project a scenario history point."""
        return cls(
            date=point.date,
            sales=float(point.sales),
            store_id=point.store_id,
            customer_traffic=point.customer_traffic,
            has_promotion=point.is_promotion,
        )


def _as_date(value: Any) -> date:
    """Coerce a matrix date cell to a ``date``."""
    if isinstance(value, date):
        return value
    try:
        import pandas as pd

        return pd.Timestamp(value).date()
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
        raise PredictionFailedError(
            "The scenario feature matrix has an unreadable date.",
            field="scenario",
        ) from exc


def _first_scalar(value: Any) -> Optional[float]:
    """Extract a single float from whatever shape an estimator returned."""
    current = value
    for _ in range(4):
        if isinstance(current, (int, float)):
            return float(current)
        try:
            length = len(current)  # type: ignore[arg-type]
        except TypeError:
            break
        if length == 0:
            return None
        current = current[0]  # type: ignore[index]
    try:
        return float(current)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = [
    "DATE_COLUMN",
    "QUANTILE_P10",
    "QUANTILE_P50",
    "QUANTILE_P90",
    "RecursiveMatrixPredictor",
    "ScenarioMatrixBuilder",
]
