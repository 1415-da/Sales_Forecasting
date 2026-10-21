"""
Baseline forecast generation for the Scenario Planner.

Produces the "do nothing" forecast that scenarios are compared against, and
records a rollback-safe checkpoint around it: the database row is written with
status ``created`` before inference, then moved to ``completed`` or ``failed``.

One pipeline, not two: prediction is delegated through the ``ModelPredictor``
port, whose default adapter calls ``ForecastService.project`` -- the same code
``POST /v1/forecast`` runs. A baseline and a forecast for the same inputs are
therefore the same number. Model resolution goes through the ``ModelResolver``
port, whose default adapter reads the MLflow registry. Both ports stay injectable
so the service remains unit testable without MLflow, a database, or a trained
model.

Two limitations are surfaced rather than hidden:

* The pipeline is a point estimator with no native quantiles, so intervals here
  are residual-derived and labelled as such.
* Only checkpoint *metadata* is persisted. Comparing a scenario against a stored
  baseline will need the points persisted too; that is a follow-up, so a reused
  ``baseline_forecast_id`` currently identifies a baseline rather than replaying
  a cached one.
"""

from __future__ import annotations

import logging
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from api.schemas.baseline_forecast import (
    UNKNOWN_LINEAGE_VALUE,
    BaselineDailyForecast,
    BaselineForecastCheckpoint,
    BaselineForecastErrorResponse,
    BaselineForecastResponse,
    BaselineForecastStatus,
    BaselineForecastSummary,
    ModelStage,
    UncertaintyMethod,
)
from api.schemas.scenario import DEFAULT_MODEL_VERSION, HistoryPoint, ScenarioRequest
from api.services.feature_schema_service import FeatureSchemaError, FeatureSchemaService
from api.services.forecast_checkpoint_service import (
    CheckpointError,
    CheckpointErrorCode,
    ForecastCheckpointService,
    new_forecast_id,
)
from api.services.scenario_validation import ScenarioValidationError, ScenarioValidationService

logger = logging.getLogger(__name__)

REQUIRE_PRODUCTION_ENV_VAR = "BASELINE_REQUIRE_PRODUCTION_STAGE"
RESIDUAL_HORIZON_SCALING_ENV_VAR = "BASELINE_RESIDUAL_HORIZON_SCALING"
MIN_RESIDUAL_CV = 0.05
MAX_RESIDUAL_CV = 0.40
DEFAULT_MODEL_TYPE = "ensemble"

CALIBRATION_WARNING = (
    "Point forecasts come from the existing inference stack, which applies a "
    "clipped level calibration to its output; treat absolute levels as indicative."
)
RESIDUAL_FALLBACK_WARNING = (
    "No model residual spread was available, so interval width was approximated "
    "from recent history volatility."
)


class BaselineForecastErrorCode(str, Enum):
    """Machine-readable codes for baseline forecast failures."""

    MODEL_VERSION_NOT_FOUND = "MODEL_VERSION_NOT_FOUND"
    MODEL_NOT_IN_PRODUCTION = "MODEL_NOT_IN_PRODUCTION"
    MLFLOW_UNAVAILABLE = "MLFLOW_UNAVAILABLE"
    FEATURE_SCHEMA_MISSING = "FEATURE_SCHEMA_MISSING"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    PREDICTION_FAILED = "PREDICTION_FAILED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"


class BaselineForecastError(Exception):
    """
    A baseline forecast could not be produced.

    Mirrors the ``error_code`` / ``message`` / ``field`` / ``details`` shape used
    by the other service errors so a route layer can handle them uniformly.
    """

    def __init__(
        self,
        error_code: BaselineForecastErrorCode | str,
        message: str,
        field_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_code: str = (
            error_code.value
            if isinstance(error_code, BaselineForecastErrorCode)
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

    def to_response(self) -> BaselineForecastErrorResponse:
        """Schema form, ready to serialise as an error body."""
        return BaselineForecastErrorResponse(**self.to_dict())

    def __repr__(self) -> str:
        return (
            f"BaselineForecastError(error_code={self.error_code!r}, "
            f"field={self.field!r}, message={self.message!r})"
        )


@dataclass(frozen=True)
class ResolvedModel:
    """The model version a baseline will be produced from."""

    model_version: str
    model_stage: str
    run_id: Optional[str] = None
    data_source: str = UNKNOWN_LINEAGE_VALUE
    dataset_version: str = UNKNOWN_LINEAGE_VALUE

    @property
    def is_production(self) -> bool:
        """Whether this version sits in the Production stage."""
        return self.model_stage == ModelStage.PRODUCTION.value


@dataclass
class PredictionResult:
    """Raw output of a predictor, before intervals are assembled."""

    predictions: List[float]
    quantiles: Dict[float, List[float]] = field(default_factory=dict)
    supports_quantiles: bool = False
    residual_std: Optional[float] = None
    warnings: List[str] = field(default_factory=list)


@runtime_checkable
class ModelResolver(Protocol):
    """Resolves a model version reference to a concrete, staged model."""

    def resolve(self, model_version: str) -> ResolvedModel:
        """Return the resolved model, or raise ``BaselineForecastError``."""
        ...


@runtime_checkable
class ModelPredictor(Protocol):
    """Produces point (and optionally quantile) forecasts."""

    def predict(
        self,
        *,
        history: Sequence[HistoryPoint],
        store_id: str,
        horizon_days: int,
        confidence_levels: Sequence[float],
        model: ResolvedModel,
    ) -> PredictionResult:
        """Forecast ``horizon_days`` ahead, or raise ``BaselineForecastError``."""
        ...


class MlflowModelResolver:
    """
    ``ModelResolver`` backed by the MLflow registry.

    ``latest`` resolves to the newest Production version of the configured
    registered model. ``mlflow`` is imported lazily so unit tests need neither the
    package nor a server.

    Note: the training DAG's promotion step currently fails silently, so the
    registry holds no Production versions. Until that is fixed, resolving
    ``latest`` here raises MODEL_VERSION_NOT_FOUND, which is the honest answer.
    """

    def __init__(
        self,
        registered_model_name: Optional[str] = None,
        tracking_uri: Optional[str] = None,
    ) -> None:
        from api.services.feature_schema_service import (
            DEFAULT_REGISTERED_MODEL_NAME,
            MLFLOW_REGISTERED_MODEL_ENV_VAR,
        )

        self.registered_model_name = registered_model_name or os.getenv(
            MLFLOW_REGISTERED_MODEL_ENV_VAR, DEFAULT_REGISTERED_MODEL_NAME
        )
        self.tracking_uri = tracking_uri or os.getenv("MLFLOW_TRACKING_URI")

    def _client(self) -> Any:
        """Build an MlflowClient, converting connection failures to a typed error."""
        try:
            import mlflow

            if self.tracking_uri:
                mlflow.set_tracking_uri(self.tracking_uri)
            return mlflow.tracking.MlflowClient()
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            raise BaselineForecastError(
                BaselineForecastErrorCode.MLFLOW_UNAVAILABLE,
                "MLflow is unavailable.",
                field_name="model_version",
                details={"tracking_uri": self.tracking_uri, "error": str(exc)},
            ) from exc

    def resolve(self, model_version: str) -> ResolvedModel:
        """Resolve a version reference (or ``latest``) to a staged model."""
        client = self._client()
        requested = (model_version or DEFAULT_MODEL_VERSION).strip()

        try:
            versions = client.search_model_versions(f"name='{self.registered_model_name}'")
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            raise BaselineForecastError(
                BaselineForecastErrorCode.MLFLOW_UNAVAILABLE,
                "Could not query the MLflow model registry.",
                field_name="model_version",
                details={
                    "registered_model_name": self.registered_model_name,
                    "error": str(exc),
                },
            ) from exc

        if requested.lower() in ("latest", "production", ""):
            production = [v for v in versions if v.current_stage == ModelStage.PRODUCTION.value]
            if not production:
                raise BaselineForecastError(
                    BaselineForecastErrorCode.MODEL_VERSION_NOT_FOUND,
                    "No Production model version is available.",
                    field_name="model_version",
                    details={
                        "registered_model_name": self.registered_model_name,
                        "available_versions": [v.version for v in versions],
                    },
                )
            chosen = max(production, key=lambda v: int(v.version))
        else:
            matches = [v for v in versions if str(v.version) == requested]
            if not matches:
                raise BaselineForecastError(
                    BaselineForecastErrorCode.MODEL_VERSION_NOT_FOUND,
                    f"Model version '{requested}' was not found.",
                    field_name="model_version",
                    details={
                        "model_version": requested,
                        "registered_model_name": self.registered_model_name,
                        "available_versions": [v.version for v in versions],
                    },
                )
            chosen = matches[0]

        lineage = self._run_lineage(client, chosen.run_id)
        return ResolvedModel(
            model_version=str(chosen.version),
            model_stage=chosen.current_stage or ModelStage.NONE.value,
            run_id=chosen.run_id,
            data_source=lineage.get("data.source", UNKNOWN_LINEAGE_VALUE),
            dataset_version=lineage.get("data.version", UNKNOWN_LINEAGE_VALUE),
        )

    @staticmethod
    def _run_lineage(client: Any, run_id: Optional[str]) -> Dict[str, str]:
        """Read the dataset lineage params the training run logged."""
        if not run_id:
            return {}
        try:
            return dict(client.get_run(run_id).data.params)
        except Exception as exc:  # noqa: BLE001 - lineage is best-effort
            logger.warning("Could not read lineage params for run %s: %s", run_id, exc)
            return {}


class CanonicalForecastPredictor:
    """
    ``ModelPredictor`` adapter over the platform's one forecasting pipeline.

    Delegates to ``ForecastService.project``, which is the same code the
    ``POST /v1/forecast`` endpoint runs: the same registry lookup, the same
    artifact load, the same feature contract, and the same recursion. A baseline
    produced here and a forecast produced there are therefore the same number,
    which is the point -- when this adapter had its own copy of the feature
    engineering (via ``ui.utils.simple_predictor``), the two could and did
    disagree, because that copy substituted out-of-distribution constants for
    the model's same-day inputs and then multiplied its output by a clipped
    correction factor.

    Only point forecasts are consumed. The pipeline exposes no quantile
    predictions, so ``supports_quantiles`` is False and the caller derives
    residual-based intervals instead.
    """

    def __init__(self, model_type: str = DEFAULT_MODEL_TYPE) -> None:
        self.model_type = model_type

    def predict(
        self,
        *,
        history: Sequence[HistoryPoint],
        store_id: str,
        horizon_days: int,
        confidence_levels: Sequence[float],
        model: ResolvedModel,
    ) -> PredictionResult:
        """Run the canonical pipeline and return its point forecasts."""
        from api.services.feature_service import LEAKAGE_WARNING
        from api.services.forecast_service import ForecastService
        from api.services.model_registry_service import ResolvedModelVersion

        # The scenario schemas carry their own `ResolvedModel`; the forecast
        # pipeline wants the registry's type. They describe the same version, so
        # this is a projection rather than a second lookup.
        resolved = ResolvedModelVersion(
            model=self.model_type,
            registered_model_name="",
            version=model.model_version,
            stage=model.model_stage,
            run_id=model.run_id,
        )

        observations = [
            _ForecastHistoryPoint(
                date=point.date,
                sales=float(point.sales),
                customer_traffic=point.customer_traffic,
            )
            for point in history
        ]

        try:
            projection = ForecastService().project(
                resolved=resolved,
                history=observations,
                store_id=store_id,
                horizon_days=horizon_days,
            )
        except BaselineForecastError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            raise BaselineForecastError(
                BaselineForecastErrorCode.PREDICTION_FAILED,
                "Baseline prediction failed.",
                details={"error": str(exc)},
            ) from exc

        return PredictionResult(
            predictions=[value for _, value in projection.predictions],
            supports_quantiles=False,
            warnings=[*projection.warnings, LEAKAGE_WARNING],
        )


@dataclass
class _ForecastHistoryPoint:
    """
    One observed day in the shape the feature builder reads.

    A small adapter rather than constructing the forecast endpoint's pydantic
    model: the builder reads attributes duck-typed, and going through the schema
    would re-run validation the scenario layer has already performed.
    """

    date: date
    sales: float
    store_id: Optional[str] = None
    quantity_sold: Optional[float] = None
    profit: Optional[float] = None
    customer_traffic: Optional[float] = None
    has_promotion: Optional[bool] = None


#: Retained name: the baseline service and any existing configuration refer to
#: this adapter, and the class it points at is a strict improvement on what it
#: used to be.
ExistingStackPredictor = CanonicalForecastPredictor


class BaselineForecastService:
    """
    Builds a reusable baseline forecast for a ``ScenarioRequest``.

    Every dependency is injectable, so the whole flow can be exercised with a
    fake model, fake predictor, and in-memory checkpoint store.
    """

    def __init__(
        self,
        model_resolver: Optional[ModelResolver] = None,
        predictor: Optional[ModelPredictor] = None,
        feature_schema_service: Optional[FeatureSchemaService] = None,
        checkpoint_service: Optional[ForecastCheckpointService] = None,
        validation_service: Optional[ScenarioValidationService] = None,
        require_production_stage: Optional[bool] = None,
        residual_horizon_scaling: Optional[bool] = None,
    ) -> None:
        self._model_resolver = model_resolver
        self._predictor = predictor
        self.feature_schema_service = feature_schema_service or FeatureSchemaService()
        self._checkpoint_service = checkpoint_service
        self.validation_service = validation_service or ScenarioValidationService()
        self.require_production_stage = (
            _env_flag(REQUIRE_PRODUCTION_ENV_VAR, default=True)
            if require_production_stage is None
            else require_production_stage
        )
        self.residual_horizon_scaling = (
            _env_flag(RESIDUAL_HORIZON_SCALING_ENV_VAR, default=True)
            if residual_horizon_scaling is None
            else residual_horizon_scaling
        )

    @property
    def model_resolver(self) -> ModelResolver:
        """Model resolver, defaulting to the MLflow registry."""
        if self._model_resolver is None:
            self._model_resolver = MlflowModelResolver()
        return self._model_resolver

    @property
    def predictor(self) -> ModelPredictor:
        """Predictor, defaulting to the existing inference stack."""
        if self._predictor is None:
            self._predictor = ExistingStackPredictor()
        return self._predictor

    @property
    def checkpoint_service(self) -> ForecastCheckpointService:
        """Checkpoint store, defaulting to the configured database."""
        if self._checkpoint_service is None:
            self._checkpoint_service = ForecastCheckpointService()
        return self._checkpoint_service

    def create_baseline(self, request: ScenarioRequest) -> BaselineForecastResponse:
        """
        Generate a baseline forecast and checkpoint it rollback-safely.

        Order matters: validate and resolve everything cheap *before* the
        checkpoint row exists, so a request that was never going to run does not
        leave a ``failed`` row behind. Once the row is ``created``, any failure
        moves it to ``failed`` and the original error is re-raised.
        """
        self._validate_history(request)
        model = self._resolve_model(request.model_version or DEFAULT_MODEL_VERSION)
        feature_schema_version = self._resolve_feature_schema_version(model)

        history_start = request.history[0].date
        history_end = request.history[-1].date
        forecast_start = history_end + timedelta(days=1)
        forecast_end = forecast_start + timedelta(days=request.horizon_days - 1)

        checkpoint = self._create_checkpoint(
            request=request,
            model=model,
            feature_schema_version=feature_schema_version,
            history_start=history_start,
            history_end=history_end,
            forecast_start=forecast_start,
            forecast_end=forecast_end,
        )

        try:
            prediction = self._predict(request, model)
            daily, method, warnings = self._build_daily_forecasts(
                request=request,
                prediction=prediction,
                forecast_start=forecast_start,
            )
            response = BaselineForecastResponse(
                baseline_forecast_id=checkpoint.forecast_id,
                store_id=request.store_id,
                model_version=model.model_version,
                model_stage=model.model_stage,
                horizon_days=request.horizon_days,
                history_start_date=history_start,
                history_end_date=history_end,
                forecast_start_date=forecast_start,
                forecast_end_date=forecast_end,
                daily_forecasts=daily,
                summary=BaselineForecastSummary.from_daily(daily),
                feature_schema_version=feature_schema_version,
                data_source=model.data_source,
                dataset_version=model.dataset_version,
                uncertainty_method=method,
                confidence_levels=list(request.confidence_levels),
                run_id=model.run_id,
                status=BaselineForecastStatus.COMPLETED,
                warnings=warnings,
            )
        except Exception as exc:
            self._mark_failed(checkpoint.forecast_id, exc)
            raise

        # Only now is the forecast complete; a failure here leaves the row in
        # `created`, never `completed`.
        self._mark_completed(checkpoint.forecast_id)
        return response

    def get_checkpoint(self, forecast_id: str) -> Optional[BaselineForecastCheckpoint]:
        """Fetch baseline metadata by id, translating database failures."""
        try:
            return self.checkpoint_service.get(forecast_id)
        except CheckpointError as exc:
            raise self._database_error(exc) from exc

    def _validate_history(self, request: ScenarioRequest) -> None:
        """Reuse the scenario validator, reporting history gaps as INSUFFICIENT_HISTORY."""
        try:
            self.validation_service.validate_history(request.history)
        except ScenarioValidationError as exc:
            raise BaselineForecastError(
                BaselineForecastErrorCode.INSUFFICIENT_HISTORY,
                exc.message,
                field_name=exc.field,
                details={"validation_error_code": exc.error_code, **exc.details},
            ) from exc

    def _resolve_model(self, model_version: str) -> ResolvedModel:
        """Resolve the model version and enforce the Production-stage policy."""
        model = self.model_resolver.resolve(model_version)

        if self.require_production_stage and not model.is_production:
            raise BaselineForecastError(
                BaselineForecastErrorCode.MODEL_NOT_IN_PRODUCTION,
                f"Model version {model.model_version} is in stage "
                f"'{model.model_stage}', not Production.",
                field_name="model_version",
                details={
                    "model_version": model.model_version,
                    "model_stage": model.model_stage,
                    "required_stage": ModelStage.PRODUCTION.value,
                    "override_env_var": REQUIRE_PRODUCTION_ENV_VAR,
                },
            )
        return model

    def _resolve_feature_schema_version(self, model: ResolvedModel) -> str:
        """Read the model's feature schema version, mapping a missing artifact."""
        reference = model.run_id or model.model_version
        try:
            return self.feature_schema_service.get_feature_schema(reference).schema_version
        except FeatureSchemaError as exc:
            raise BaselineForecastError(
                BaselineForecastErrorCode.FEATURE_SCHEMA_MISSING,
                exc.message,
                field_name="model_version",
                details={"feature_schema_error_code": exc.error_code, **exc.details},
            ) from exc

    def _create_checkpoint(
        self,
        *,
        request: ScenarioRequest,
        model: ResolvedModel,
        feature_schema_version: str,
        history_start: date,
        history_end: date,
        forecast_start: date,
        forecast_end: date,
    ) -> BaselineForecastCheckpoint:
        """Write the ``created`` row before any inference runs."""
        try:
            return self.checkpoint_service.create(
                forecast_id=new_forecast_id(),
                store_id=request.store_id,
                model_version=model.model_version,
                model_stage=model.model_stage,
                horizon_days=request.horizon_days,
                history_start_date=history_start,
                history_end_date=history_end,
                forecast_start_date=forecast_start,
                forecast_end_date=forecast_end,
                data_source=model.data_source,
                dataset_version=model.dataset_version,
                feature_schema_version=feature_schema_version,
            )
        except CheckpointError as exc:
            raise self._database_error(exc) from exc

    def _predict(self, request: ScenarioRequest, model: ResolvedModel) -> PredictionResult:
        """Run the predictor, wrapping unexpected failures."""
        try:
            prediction = self.predictor.predict(
                history=request.history,
                store_id=request.store_id,
                horizon_days=request.horizon_days,
                confidence_levels=list(request.confidence_levels),
                model=model,
            )
        except BaselineForecastError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            raise BaselineForecastError(
                BaselineForecastErrorCode.PREDICTION_FAILED,
                "Baseline prediction failed.",
                details={"error": str(exc)},
            ) from exc

        if len(prediction.predictions) != request.horizon_days:
            raise BaselineForecastError(
                BaselineForecastErrorCode.PREDICTION_FAILED,
                "Predictor returned the wrong number of forecast days.",
                details={
                    "expected": request.horizon_days,
                    "received": len(prediction.predictions),
                },
            )
        if any(value is None or not math.isfinite(value) for value in prediction.predictions):
            raise BaselineForecastError(
                BaselineForecastErrorCode.PREDICTION_FAILED,
                "Predictor returned non-finite values.",
                details={"horizon_days": request.horizon_days},
            )
        return prediction

    def _build_daily_forecasts(
        self,
        *,
        request: ScenarioRequest,
        prediction: PredictionResult,
        forecast_start: date,
    ):
        """
        Assemble per-day rows with intervals.

        Native quantiles are used when the predictor provides them
        (``quantile_model``); otherwise bounds come from residual spread
        (``residual_based``).
        """
        warnings = list(prediction.warnings)
        levels = sorted(request.confidence_levels)
        lower_level, upper_level = levels[0], levels[-1]

        if prediction.supports_quantiles and prediction.quantiles:
            method = UncertaintyMethod.QUANTILE_MODEL
            bounds = self._quantile_bounds(prediction, lower_level, upper_level)
        else:
            method = UncertaintyMethod.RESIDUAL_BASED
            residual_std, fell_back = self._residual_std(request, prediction)
            if fell_back:
                warnings.append(RESIDUAL_FALLBACK_WARNING)
            bounds = self._residual_bounds(
                prediction.predictions, residual_std, lower_level, upper_level
            )

        daily: List[BaselineDailyForecast] = []
        for index, value in enumerate(prediction.predictions):
            lower, upper = bounds[index]
            daily.append(
                BaselineDailyForecast(
                    date=forecast_start + timedelta(days=index),
                    predicted_sales=value,
                    lower_bound=lower,
                    upper_bound=upper,
                    p10=self._quantile_at(prediction, 0.1, index, default=lower),
                    p50=self._quantile_at(prediction, 0.5, index, default=value),
                    p90=self._quantile_at(prediction, 0.9, index, default=upper),
                    uncertainty_method=method,
                )
            )
        return daily, method, warnings

    @staticmethod
    def _quantile_bounds(
        prediction: PredictionResult, lower_level: float, upper_level: float
    ) -> List[tuple]:
        """Bounds taken straight from the predictor's quantiles."""
        lower_series = prediction.quantiles.get(lower_level)
        upper_series = prediction.quantiles.get(upper_level)
        bounds = []
        for index, value in enumerate(prediction.predictions):
            lower = lower_series[index] if lower_series else value
            upper = upper_series[index] if upper_series else value
            bounds.append((max(0.0, min(lower, upper)), max(lower, upper)))
        return bounds

    def _residual_bounds(
        self,
        predictions: Sequence[float],
        residual_std: float,
        lower_level: float,
        upper_level: float,
    ) -> List[tuple]:
        """
        Approximate bounds from residual spread.

        Width grows with the square root of the step, the standard random-walk
        error-growth assumption, so far-horizon days are not reported as being
        as certain as day one. Disable with
        ``BASELINE_RESIDUAL_HORIZON_SCALING=false``.
        """
        normal = statistics.NormalDist()
        z_lower = normal.inv_cdf(lower_level)
        z_upper = normal.inv_cdf(upper_level)

        bounds = []
        for index, value in enumerate(predictions):
            scale = math.sqrt(index + 1) if self.residual_horizon_scaling else 1.0
            spread = residual_std * scale
            lower = value + z_lower * spread
            upper = value + z_upper * spread
            bounds.append((max(0.0, min(lower, upper)), max(lower, upper)))
        return bounds

    @staticmethod
    def _residual_std(request: ScenarioRequest, prediction: PredictionResult):
        """
        Residual standard deviation, preferring the model's own.

        Falls back to recent history volatility, clamped to a sane coefficient of
        variation so the interval is neither invisible nor absurd.
        """
        if prediction.residual_std is not None and prediction.residual_std > 0:
            return float(prediction.residual_std), False

        sales = [float(point.sales) for point in request.history]
        mean = statistics.fmean(sales) if sales else 0.0
        stdev = statistics.stdev(sales) if len(sales) > 1 else 0.0

        if mean <= 0:
            return (stdev or 1.0), True

        cv = min(max(stdev / mean, MIN_RESIDUAL_CV), MAX_RESIDUAL_CV)
        return mean * cv, True

    @staticmethod
    def _quantile_at(
        prediction: PredictionResult, level: float, index: int, default: Optional[float]
    ) -> Optional[float]:
        """One quantile for one day, or the supplied default."""
        if not prediction.supports_quantiles:
            return None
        series = prediction.quantiles.get(level)
        if not series or index >= len(series):
            return default
        return float(series[index])

    def _mark_completed(self, forecast_id: str) -> None:
        """Promote the checkpoint to ``completed``."""
        try:
            self.checkpoint_service.mark_completed(forecast_id)
        except CheckpointError as exc:
            raise self._database_error(exc) from exc

    def _mark_failed(self, forecast_id: str, exc: Exception) -> None:
        """
        Record the failure without masking it.

        A database problem while recording the failure is logged, never raised:
        the caller must see the original error.
        """
        error_code = getattr(exc, "error_code", type(exc).__name__)
        message = self._failure_message(exc)
        try:
            self.checkpoint_service.mark_failed(
                forecast_id, error_code=str(error_code), error_message=str(message)
            )
        except CheckpointError as checkpoint_exc:
            logger.error(
                "Could not mark baseline %s as failed (%s); original error: %s",
                forecast_id,
                checkpoint_exc.message,
                message,
            )

    @staticmethod
    def _failure_message(exc: Exception) -> str:
        """
        Build the message stored on a failed checkpoint.

        Wrapper errors carry a generic message ("Baseline prediction failed.")
        and keep the root cause in ``details["error"]``. Both are persisted, or a
        failed baseline is undiagnosable after the fact.
        """
        message = getattr(exc, "message", None) or str(exc)
        cause = (getattr(exc, "details", None) or {}).get("error")
        if cause and str(cause) not in message:
            return f"{message} Cause: {cause}"
        return message

    @staticmethod
    def _database_error(exc: CheckpointError) -> BaselineForecastError:
        """Translate a checkpoint failure into the baseline error vocabulary."""
        code = (
            BaselineForecastErrorCode.DATABASE_UNAVAILABLE
            if exc.error_code
            in (
                CheckpointErrorCode.DATABASE_UNAVAILABLE.value,
                CheckpointErrorCode.DATABASE_NOT_CONFIGURED.value,
            )
            else exc.error_code
        )
        return BaselineForecastError(
            code,
            exc.message,
            field_name=exc.field,
            details={"checkpoint_error_code": exc.error_code, **exc.details},
        )


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment flag."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
