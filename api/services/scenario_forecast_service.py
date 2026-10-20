"""
Scenario comparison orchestration.

Ties the pieces together: resolve or generate a baseline, apply the scenario's
feature overrides, run the model on both matrices, and compute the impact.

Two ports are declared here because the pieces they stand for do not exist yet:

* ``FutureFeatureBuilder`` -- nothing in the codebase builds a forecast-time
  feature matrix. ``SimplePredictor`` builds features row-by-row inside its own
  recursive loop, so there is no matrix to hand to the override engine.
* ``FeatureMatrixPredictor`` -- the existing stack predicts from history, not
  from a prepared matrix.

Both are injected, so this service and its tests are complete today, but wiring
it to a real model requires implementing those two adapters. That work depends
on the train/serve feature contract, which is why it is deliberately not
guessed at here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

import pandas as pd

from api.schemas.baseline_forecast import (
    BaselineDailyForecast,
    BaselineForecastCheckpoint,
    BaselineForecastResponse,
    UncertaintyMethod,
)
from api.schemas.scenario import HistoryPoint, ScenarioRequest
from api.schemas.scenario_capabilities import FeatureSchema
from api.schemas.scenario_comparison import (
    QUANTILE_P10,
    QUANTILE_P50,
    QUANTILE_P90,
    SCENARIO_LIMITATIONS,
    DailyImpact,
    ForecastSeriesSummary,
    ForecastType,
    ScenarioComparisonErrorResponse,
    ScenarioComparisonResponse,
    new_scenario_forecast_id,
    new_scenario_id,
)
from api.observability.scenario_logging import (
    ScenarioLogContext,
    StageTimer,
    log_baseline_resolved,
    log_impact_calculated,
    log_overrides_applied,
    log_prediction_completed,
)
from api.services.baseline_forecast_service import (
    BaselineForecastService,
    ResolvedModel,
)
from api.services.feature_schema_service import FeatureSchemaService
from api.services.forecast_checkpoint_service import CheckpointError, ForecastCheckpointService
from api.services.scenario_feature_service import ScenarioFeatureService
from api.services.scenario_impact_service import ScenarioImpactService

logger = logging.getLogger(__name__)

BASELINE_RECOMPUTED_WARNING = (
    "Baseline forecast points are not persisted, so the baseline was recomputed "
    "from its checkpoint under the same baseline_forecast_id."
)
UNSUPPORTED_OVERRIDE_WARNING = (
    "Some requested overrides were not applied because the model's feature schema "
    "does not contain them; the scenario is therefore partial."
)


class ScenarioComparisonErrorCode(str, Enum):
    """Machine-readable codes for comparison orchestration failures."""

    BASELINE_NOT_FOUND = "BASELINE_NOT_FOUND"
    BASELINE_NOT_REUSABLE = "BASELINE_NOT_REUSABLE"
    BASELINE_REQUEST_MISMATCH = "BASELINE_REQUEST_MISMATCH"
    FEATURE_MATRIX_UNAVAILABLE = "FEATURE_MATRIX_UNAVAILABLE"
    PREDICTION_FAILED = "PREDICTION_FAILED"


class ScenarioComparisonError(Exception):
    """
    A comparison could not be produced.

    Mirrors the ``error_code`` / ``message`` / ``field`` / ``details`` shape used
    by the other service errors so a route layer can handle them uniformly.
    """

    def __init__(
        self,
        error_code: ScenarioComparisonErrorCode | str,
        message: str,
        field_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_code: str = (
            error_code.value
            if isinstance(error_code, ScenarioComparisonErrorCode)
            else error_code
        )
        self.message = message
        self.field = field_name
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

    def to_response(self) -> ScenarioComparisonErrorResponse:
        """Schema form, ready to serialise as an error body."""
        return ScenarioComparisonErrorResponse(**self.to_dict())

    def __repr__(self) -> str:
        return (
            f"ScenarioComparisonError(error_code={self.error_code!r}, "
            f"field={self.field!r}, message={self.message!r})"
        )


@dataclass
class MatrixPrediction:
    """Model output for one feature matrix."""

    dates: List[date]
    predictions: List[float]
    quantiles: Dict[float, List[float]] = field(default_factory=dict)
    uncertainty_method: UncertaintyMethod = UncertaintyMethod.RESIDUAL_BASED

    @property
    def has_quantiles(self) -> bool:
        """Whether P10/P50/P90 are all present."""
        return all(
            level in self.quantiles for level in (QUANTILE_P10, QUANTILE_P50, QUANTILE_P90)
        )


@dataclass
class ResolvedBaseline:
    """The baseline a comparison will be measured against."""

    baseline_forecast_id: str
    model_version: str
    model_stage: str
    forecast_start_date: date
    forecast_end_date: date
    horizon_days: int
    run_id: Optional[str] = None
    reused: bool = False
    response: Optional[BaselineForecastResponse] = None
    checkpoint: Optional[BaselineForecastCheckpoint] = None

    @property
    def model(self) -> ResolvedModel:
        """Model reference for the feature builder and predictor."""
        return ResolvedModel(
            model_version=self.model_version,
            model_stage=self.model_stage,
            run_id=self.run_id,
        )


@runtime_checkable
class FutureFeatureBuilder(Protocol):
    """Builds the baseline forecast-time feature matrix."""

    def build(
        self,
        *,
        history: Sequence[HistoryPoint],
        store_id: str,
        forecast_start_date: date,
        forecast_end_date: date,
        feature_schema: FeatureSchema,
        model: ResolvedModel,
    ) -> pd.DataFrame:
        """Return one row per forecast day, with the model's feature columns."""
        ...


@runtime_checkable
class FeatureMatrixPredictor(Protocol):
    """Runs a model over a prepared feature matrix."""

    def predict_matrix(
        self,
        *,
        features: pd.DataFrame,
        model: ResolvedModel,
        confidence_levels: Sequence[float],
    ) -> MatrixPrediction:
        """Predict every row of ``features``."""
        ...


class CheckpointBaselineProvider:
    """
    Resolves a supplied ``baseline_forecast_id`` through the checkpoint store.

    Only checkpoint *metadata* is persisted today, so a reused baseline is
    validated here and then recomputed by the caller under the same id. When a
    points store is added, this is the single place that needs to return a full
    ``BaselineForecastResponse`` instead.
    """

    def __init__(self, checkpoint_service: Optional[ForecastCheckpointService] = None) -> None:
        self._checkpoint_service = checkpoint_service

    @property
    def checkpoint_service(self) -> ForecastCheckpointService:
        """Checkpoint store, defaulting to the configured database."""
        if self._checkpoint_service is None:
            self._checkpoint_service = ForecastCheckpointService()
        return self._checkpoint_service

    def resolve(self, baseline_forecast_id: str, request: ScenarioRequest) -> ResolvedBaseline:
        """Validate the checkpoint and describe the baseline it refers to."""
        try:
            checkpoint = self.checkpoint_service.get(baseline_forecast_id)
        except CheckpointError as exc:
            raise ScenarioComparisonError(
                exc.error_code,
                exc.message,
                field_name="baseline_forecast_id",
                details=exc.details,
            ) from exc

        if checkpoint is None:
            raise ScenarioComparisonError(
                ScenarioComparisonErrorCode.BASELINE_NOT_FOUND,
                f"Baseline forecast '{baseline_forecast_id}' was not found.",
                field_name="baseline_forecast_id",
                details={"baseline_forecast_id": baseline_forecast_id},
            )

        if not checkpoint.is_reusable:
            raise ScenarioComparisonError(
                ScenarioComparisonErrorCode.BASELINE_NOT_REUSABLE,
                f"Baseline forecast '{baseline_forecast_id}' is "
                f"{checkpoint.status.value} and cannot be reused.",
                field_name="baseline_forecast_id",
                details={
                    "baseline_forecast_id": baseline_forecast_id,
                    "status": checkpoint.status.value,
                    "expired": checkpoint.is_expired,
                },
            )

        self._validate_matches_request(checkpoint, request)
        return ResolvedBaseline(
            baseline_forecast_id=checkpoint.forecast_id,
            model_version=checkpoint.model_version,
            model_stage=checkpoint.model_stage,
            forecast_start_date=checkpoint.forecast_start_date,
            forecast_end_date=checkpoint.forecast_end_date,
            horizon_days=checkpoint.horizon_days,
            reused=True,
            checkpoint=checkpoint,
        )

    @staticmethod
    def _validate_matches_request(
        checkpoint: BaselineForecastCheckpoint, request: ScenarioRequest
    ) -> None:
        """A baseline for another store or horizon is not a valid comparison base."""
        mismatches: Dict[str, Any] = {}
        if checkpoint.store_id != request.store_id:
            mismatches["store_id"] = {
                "baseline": checkpoint.store_id,
                "requested": request.store_id,
            }
        if checkpoint.horizon_days != request.horizon_days:
            mismatches["horizon_days"] = {
                "baseline": checkpoint.horizon_days,
                "requested": request.horizon_days,
            }
        if request.history and checkpoint.history_end_date != request.history[-1].date:
            mismatches["history_end_date"] = {
                "baseline": checkpoint.history_end_date.isoformat(),
                "requested": request.history[-1].date.isoformat(),
            }

        if mismatches:
            raise ScenarioComparisonError(
                ScenarioComparisonErrorCode.BASELINE_REQUEST_MISMATCH,
                "The supplied baseline does not match this scenario request.",
                field_name="baseline_forecast_id",
                details={
                    "baseline_forecast_id": checkpoint.forecast_id,
                    "mismatches": mismatches,
                },
            )


class ScenarioForecastService:
    """
    Produces a complete baseline-vs-scenario comparison.

    Every collaborator is injectable so the whole flow is testable without a
    model, MLflow, or a database.
    """

    def __init__(
        self,
        feature_builder: FutureFeatureBuilder,
        matrix_predictor: FeatureMatrixPredictor,
        baseline_service: Optional[BaselineForecastService] = None,
        feature_service: Optional[ScenarioFeatureService] = None,
        impact_service: Optional[ScenarioImpactService] = None,
        feature_schema_service: Optional[FeatureSchemaService] = None,
        baseline_provider: Optional[CheckpointBaselineProvider] = None,
    ) -> None:
        self.feature_builder = feature_builder
        self.matrix_predictor = matrix_predictor
        self._baseline_service = baseline_service
        self.feature_service = feature_service or ScenarioFeatureService()
        self.impact_service = impact_service or ScenarioImpactService()
        self._feature_schema_service = feature_schema_service
        self._baseline_provider = baseline_provider

    @property
    def baseline_service(self) -> BaselineForecastService:
        """Baseline generator, defaulting to the configured service."""
        if self._baseline_service is None:
            self._baseline_service = BaselineForecastService()
        return self._baseline_service

    @property
    def feature_schema_service(self) -> FeatureSchemaService:
        """Feature schema reader, defaulting to the MLflow-backed service."""
        if self._feature_schema_service is None:
            self._feature_schema_service = FeatureSchemaService()
        return self._feature_schema_service

    @property
    def baseline_provider(self) -> CheckpointBaselineProvider:
        """Baseline resolver, defaulting to the checkpoint store."""
        if self._baseline_provider is None:
            self._baseline_provider = CheckpointBaselineProvider()
        return self._baseline_provider

    def create_comparison(
        self,
        request: ScenarioRequest,
        log_context: Optional[ScenarioLogContext] = None,
    ) -> ScenarioComparisonResponse:
        """
        Run a what-if comparison for ``request``.

        Both sides are predicted from feature matrices built by the same
        pipeline, so the only difference between them is the overrides the
        scenario applied.

        ``log_context`` is optional: when supplied, each pipeline stage emits a
        structured event with its own measured duration. Observability is
        additive here and never alters the result.
        """
        warnings: List[str] = []
        timer = StageTimer()

        baseline = self._resolve_baseline(request, warnings)
        feature_schema = self._load_feature_schema(baseline)
        if log_context is not None:
            log_baseline_resolved(
                log_context,
                duration_ms=timer.elapsed_ms(),
                reused=baseline.reused,
                baseline_forecast_id=baseline.baseline_forecast_id,
            )

        timer.reset()
        baseline_matrix = self._build_matrix(request, baseline, feature_schema)
        override_result = self.feature_service.apply_scenario(
            future_features=baseline_matrix,
            scenario=request.scenario,
            feature_schema=feature_schema,
            forecast_start_date=baseline.forecast_start_date,
            forecast_end_date=baseline.forecast_end_date,
        )
        if override_result.unsupported_overrides:
            warnings.append(UNSUPPORTED_OVERRIDE_WARNING)
        if log_context is not None:
            log_overrides_applied(
                log_context,
                duration_ms=timer.elapsed_ms(),
                applied_features=list(override_result.modified_feature_names),
                unsupported_features=[
                    item.feature_name for item in override_result.unsupported_overrides
                ],
                modified_days=len(override_result.modified_dates),
            )

        timer.reset()
        baseline_prediction = self._predict(baseline_matrix, baseline, request, "baseline")
        scenario_prediction = self._predict(
            override_result.scenario_future_features, baseline, request, "scenario"
        )
        if log_context is not None:
            log_prediction_completed(
                log_context,
                duration_ms=timer.elapsed_ms(),
                horizon_days=baseline.horizon_days,
                has_quantiles=scenario_prediction.has_quantiles,
            )

        timer.reset()
        baseline_daily = self._to_daily(baseline_prediction)
        scenario_daily = self._to_daily(scenario_prediction)
        impact = self.impact_service.compute_impact(baseline_daily, scenario_daily)
        if log_context is not None:
            log_impact_calculated(
                log_context,
                duration_ms=timer.elapsed_ms(),
                total_difference=impact.total_difference,
                total_difference_pct=impact.total_difference_pct,
                positive_days=impact.positive_days,
                negative_days=impact.negative_days,
            )

        return ScenarioComparisonResponse(
            scenario_id=new_scenario_id(),
            baseline_forecast_id=baseline.baseline_forecast_id,
            scenario_forecast_id=new_scenario_forecast_id(),
            model_version=baseline.model_version,
            model_stage=baseline.model_stage,
            scenario_type=request.scenario.scenario_type,
            forecast_type=ForecastType.PREDICTIVE_WHAT_IF,
            baseline=self._summarise(
                baseline_daily, baseline_prediction, baseline.baseline_forecast_id
            ),
            scenario=self._summarise(
                scenario_daily, scenario_prediction, forecast_id=None
            ),
            impact=impact,
            daily_forecasts=list(impact.daily_impacts),
            applied_overrides=list(override_result.applied_overrides),
            unsupported_overrides=list(override_result.unsupported_overrides),
            modified_features=list(override_result.modified_feature_names),
            modified_dates=list(override_result.modified_dates),
            limitations=list(SCENARIO_LIMITATIONS),
            store_id=request.store_id,
            assumptions=request.scenario,
            baseline_reused=baseline.reused,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _resolve_baseline(
        self, request: ScenarioRequest, warnings: List[str]
    ) -> ResolvedBaseline:
        """Reuse the supplied baseline when valid, otherwise generate one."""
        if request.baseline_forecast_id:
            baseline = self.baseline_provider.resolve(request.baseline_forecast_id, request)
            if baseline.response is None:
                warnings.append(BASELINE_RECOMPUTED_WARNING)
            return baseline

        response = self.baseline_service.create_baseline(request)
        return ResolvedBaseline(
            baseline_forecast_id=response.baseline_forecast_id,
            model_version=response.model_version,
            model_stage=response.model_stage,
            forecast_start_date=response.forecast_start_date,
            forecast_end_date=response.forecast_end_date,
            horizon_days=response.horizon_days,
            run_id=response.run_id,
            reused=False,
            response=response,
        )

    def _load_feature_schema(self, baseline: ResolvedBaseline) -> FeatureSchema:
        """Read the feature schema of the baseline's model."""
        reference = baseline.run_id or baseline.model_version
        return self.feature_schema_service.get_feature_schema(reference)

    def _build_matrix(
        self,
        request: ScenarioRequest,
        baseline: ResolvedBaseline,
        feature_schema: FeatureSchema,
    ) -> pd.DataFrame:
        """Build the baseline future feature matrix and sanity-check its shape."""
        try:
            matrix = self.feature_builder.build(
                history=request.history,
                store_id=request.store_id,
                forecast_start_date=baseline.forecast_start_date,
                forecast_end_date=baseline.forecast_end_date,
                feature_schema=feature_schema,
                model=baseline.model,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            raise ScenarioComparisonError(
                ScenarioComparisonErrorCode.FEATURE_MATRIX_UNAVAILABLE,
                "The baseline feature matrix could not be built.",
                details={"error": str(exc)},
            ) from exc

        if matrix is None or matrix.empty:
            raise ScenarioComparisonError(
                ScenarioComparisonErrorCode.FEATURE_MATRIX_UNAVAILABLE,
                "The baseline feature matrix is empty.",
                details={
                    "forecast_start_date": baseline.forecast_start_date.isoformat(),
                    "forecast_end_date": baseline.forecast_end_date.isoformat(),
                },
            )
        return matrix

    def _predict(
        self,
        matrix: pd.DataFrame,
        baseline: ResolvedBaseline,
        request: ScenarioRequest,
        label: str,
    ) -> MatrixPrediction:
        """
        Predict the forecast window of one matrix.

        History rows are filtered out first, so a matrix that carries them can
        never contribute predicted days to the comparison.
        """
        window = self._forecast_window(matrix, baseline)
        try:
            prediction = self.matrix_predictor.predict_matrix(
                features=window,
                model=baseline.model,
                confidence_levels=list(request.confidence_levels),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            raise ScenarioComparisonError(
                ScenarioComparisonErrorCode.PREDICTION_FAILED,
                f"The {label} forecast could not be predicted.",
                details={"error": str(exc), "side": label},
            ) from exc

        if len(prediction.predictions) != len(prediction.dates):
            raise ScenarioComparisonError(
                ScenarioComparisonErrorCode.PREDICTION_FAILED,
                f"The {label} prediction returned mismatched dates and values.",
                details={
                    "dates": len(prediction.dates),
                    "values": len(prediction.predictions),
                    "side": label,
                },
            )
        return prediction

    @staticmethod
    def _forecast_window(matrix: pd.DataFrame, baseline: ResolvedBaseline) -> pd.DataFrame:
        """Rows of the matrix inside the forecast horizon."""
        if "date" not in matrix.columns:
            raise ScenarioComparisonError(
                ScenarioComparisonErrorCode.FEATURE_MATRIX_UNAVAILABLE,
                "The feature matrix has no 'date' column.",
                details={"columns": list(matrix.columns)},
            )
        dates = pd.to_datetime(matrix["date"]).dt.date
        mask = (dates >= baseline.forecast_start_date) & (
            dates <= baseline.forecast_end_date
        )
        return matrix.loc[mask.values].reset_index(drop=True)

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _to_daily(prediction: MatrixPrediction) -> List[BaselineDailyForecast]:
        """
        Convert a matrix prediction to daily rows.

        Bounds come from the model's own P10/P90 when present; otherwise the
        point forecast is used for both, and the band is left to the baseline
        service rather than invented here.
        """
        p10 = prediction.quantiles.get(QUANTILE_P10)
        p50 = prediction.quantiles.get(QUANTILE_P50)
        p90 = prediction.quantiles.get(QUANTILE_P90)

        rows: List[BaselineDailyForecast] = []
        for index, (day, value) in enumerate(zip(prediction.dates, prediction.predictions)):
            lower = float(p10[index]) if p10 else float(value)
            upper = float(p90[index]) if p90 else float(value)
            rows.append(
                BaselineDailyForecast(
                    date=day,
                    predicted_sales=float(value),
                    lower_bound=min(lower, upper),
                    upper_bound=max(lower, upper),
                    p10=float(p10[index]) if p10 else None,
                    p50=float(p50[index]) if p50 else None,
                    p90=float(p90[index]) if p90 else None,
                    uncertainty_method=prediction.uncertainty_method,
                )
            )
        return rows

    @staticmethod
    def _summarise(
        daily: Sequence[BaselineDailyForecast],
        prediction: MatrixPrediction,
        forecast_id: Optional[str],
    ) -> ForecastSeriesSummary:
        """Aggregate one side of the comparison."""
        if not daily:
            return ForecastSeriesSummary(forecast_id=forecast_id, horizon_days=0)

        values = [row.predicted_sales for row in daily]
        return ForecastSeriesSummary(
            forecast_id=forecast_id,
            horizon_days=len(daily),
            start_date=daily[0].date,
            end_date=daily[-1].date,
            total_forecast=sum(values),
            average_daily_forecast=sum(values) / len(values),
            min_daily_forecast=min(values),
            max_daily_forecast=max(values),
            uncertainty_method=prediction.uncertainty_method,
            has_quantiles=prediction.has_quantiles,
        )
