"""
Multi-day forecast generation.

The one place a forecast is produced. Composes the registry (which version
serves), the artifact service (load it), and the feature builder (what to feed
it) into a recursive day-by-day projection with an uncertainty band.

Recursion
---------
Each forecast day's lag and rolling inputs are computed from the observed
history extended by the days already predicted in this run, which is how a
single-step model produces a multi-day path. The consequence is that error
compounds with horizon, and the interval widens accordingly.

Uncertainty
-----------
The trained models are point estimators -- no quantile regression, no
native prediction intervals -- so the band is constructed, and the method is
reported in ``metadata.uncertainty_method`` rather than implied:

* ``residual_normal`` (preferred): a normal interval around the point forecast
  using the model's own held-out RMSE as the residual standard deviation, taken
  from the metric the training run logged for this model. Widened by
  ``sqrt(step)`` because a recursive forecast feeds its own error forward.
* ``history_volatility`` (fallback): when the run logged no RMSE, the residual
  spread is approximated from the standard deviation of recent history. This is
  a proxy for model error, not a measurement of it, and says so in a warning.

No output calibration is applied. The previous Streamlit path multiplied its
predictions by a clipped ratio to pull them back towards recent history; that
was compensating for out-of-distribution exogenous inputs, which
``feature_service`` now avoids at the source.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from api.config import Settings, get_settings, model_label
from api.core.errors import PredictionFailedError
from api.schemas.forecast import (
    FEATURE_SCHEMA_VERSION,
    UNCERTAINTY_METHOD_HISTORY,
    UNCERTAINTY_METHOD_RESIDUAL,
    DailyForecast,
    ForecastMetadata,
    ForecastRequest,
    ForecastResponse,
    ForecastSummary,
    utc_now,
)
from api.schemas.models import ModelDetailResponse, ModelInfo, ModelListResponse, ModelMetrics
from api.services.artifact_service import ArtifactService, ModelBundle
from api.services.feature_service import LEAKAGE_WARNING, FeatureBuilder
from api.services.model_registry_service import (
    ModelRegistryService,
    ResolvedModelVersion,
)

logger = logging.getLogger(__name__)

#: Two-sided normal critical values for the confidence levels on offer.
Z_SCORES: Dict[float, float] = {0.8: 1.2815515655446004, 0.95: 1.959963984540054}

#: Floor on the residual spread, as a fraction of the recent sales level. A
#: model that reports a near-zero RMSE would otherwise produce a band of
#: visually zero width, overstating how precise a recursive forecast can be.
MIN_RESIDUAL_FRACTION = 0.02

#: Ceiling on the same, so a pathological RMSE cannot produce a band so wide it
#: carries no information.
MAX_RESIDUAL_FRACTION = 0.5

FORECAST_ID_PREFIX = "fc_"

HISTORY_VOLATILITY_WARNING = (
    "The training run recorded no error metric for this model, so the "
    "uncertainty band was approximated from the volatility of the supplied "
    "history. It reflects how variable recent sales were, not how accurate the "
    "model is."
)
RECURSIVE_WARNING = (
    "Days beyond the first are forecast from earlier forecasts, so uncertainty "
    "grows across the horizon."
)


@dataclass
class Projection:
    """
    A point forecast and the context it was produced in.

    Returned by ``project`` so a caller that needs only the point series -- the
    Scenario Planner's baseline leg, for instance -- can reuse the whole
    pipeline (registry, artifacts, feature contract, recursion) without going
    through the HTTP response assembly.
    """

    predictions: List[Tuple[date, float]]
    bundle: ModelBundle
    series: Any
    policy: Any
    warnings: List[str]


@dataclass
class _Uncertainty:
    """The residual spread used to build the band, and how it was obtained."""

    residual_std: float
    method: str
    warnings: List[str]


class ForecastService:
    """
    Generates forecasts and describes the models that produce them.

    Every collaborator is injectable, so the whole flow is testable with a fake
    registry and a stub estimator -- no MLflow, no MinIO, no trained model.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[ModelRegistryService] = None,
        artifacts: Optional[ArtifactService] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._registry = registry
        self._artifacts = artifacts

    @property
    def registry(self) -> ModelRegistryService:
        """Model registry access, built on first use."""
        if self._registry is None:
            self._registry = ModelRegistryService(self.settings)
        return self._registry

    @property
    def artifacts(self) -> ArtifactService:
        """Artifact access, built on first use."""
        if self._artifacts is None:
            self._artifacts = ArtifactService(self.settings)
        return self._artifacts

    # -- model discovery ---------------------------------------------------

    def list_models(self) -> ModelListResponse:
        """
        Describe every configured model's serving version.

        Store lists come from each model's encoder artifact, so the UI offers
        exactly the stores the model was trained on. An artifact that cannot be
        read degrades to an empty list plus a warning rather than failing the
        whole listing -- a model selector is still useful without it.
        """
        serving, warnings = self.registry.list_serving_versions()
        infos: List[ModelInfo] = []
        all_stores: List[str] = []

        for resolved in serving:
            bundle = (
                self.artifacts.try_load_bundle(resolved.run_id, resolved.model)
                if resolved.run_id
                else None
            )
            if bundle is None and resolved.run_id:
                warnings.append(
                    f"Artifacts for '{resolved.model}' version {resolved.version} "
                    "could not be read, so its feature and store details are "
                    "unavailable."
                )
            infos.append(self._to_info(resolved, bundle))
            for store in bundle.supported_stores if bundle else []:
                if store not in all_stores:
                    all_stores.append(store)

        production = [info for info in infos if info.is_production]
        default = self.settings.default_model
        if not any(info.model == default for info in infos):
            default = infos[0].model if infos else None
        for info in infos:
            info.is_default = info.model == default

        return ModelListResponse(
            models=infos,
            default_model=default,
            production_available=bool(production),
            supported_stores=all_stores,
            warnings=warnings,
        )

    def get_model(
        self, model_version: str, model: Optional[str] = None
    ) -> ModelDetailResponse:
        """Detail for one version of one model, including its feature list."""
        resolved = self.registry.resolve(model or self.settings.default_model, model_version)
        bundle = (
            self.artifacts.try_load_bundle(resolved.run_id, resolved.model)
            if resolved.run_id
            else None
        )
        warnings: List[str] = []
        if bundle is None:
            warnings.append(
                "Model artifacts could not be read, so the feature list and "
                "store list are unavailable."
            )
        return ModelDetailResponse(
            model=self._to_info(resolved, bundle),
            features=list(bundle.feature_cols) if bundle else [],
            lineage=resolved.lineage(),
            warnings=warnings,
        )

    def _to_info(
        self, resolved: ResolvedModelVersion, bundle: Optional[ModelBundle]
    ) -> ModelInfo:
        """Project a resolved version (and its artifacts, if readable) to the wire type."""
        lineage = resolved.lineage()
        return ModelInfo(
            model=resolved.model,
            label=model_label(resolved.model),
            registered_model_name=resolved.registered_model_name,
            version=resolved.version,
            stage=resolved.stage,
            run_id=resolved.run_id,
            is_production=resolved.is_production,
            metrics=ModelMetrics(**resolved.metrics),
            training_date=resolved.training_date,
            supported_stores=bundle.supported_stores if bundle else [],
            feature_count=bundle.feature_count if bundle else None,
            data_source=lineage.get("data_source"),
            dataset_version=lineage.get("dataset_version"),
        )

    # -- forecasting -------------------------------------------------------

    def generate(self, request: ForecastRequest) -> ForecastResponse:
        """
        Produce the forecast for a validated request.

        Ordered so that everything cheap and fallible happens first: resolve the
        version, load the artifacts, check the history against the model's
        feature contract, and only then run inference.
        """
        resolved = self.registry.resolve(request.model, request.model_version)
        projection = self.project(
            resolved=resolved,
            history=request.history,
            store_id=request.store_id,
            horizon_days=request.horizon_days,
        )
        bundle = projection.bundle
        series = projection.series
        policy = projection.policy
        warnings = list(projection.warnings)
        predictions = projection.predictions

        uncertainty = self._resolve_uncertainty(resolved, series.sales, predictions)
        bands = self._build_bands(predictions, request.confidence_level, uncertainty)

        warnings.append(LEAKAGE_WARNING)
        warnings.append(RECURSIVE_WARNING)
        warnings.extend(uncertainty.warnings)
        if not resolved.is_production:
            warnings.append(
                f"Version {resolved.version} is in stage '{resolved.stage}', not "
                "Production."
            )

        lineage = resolved.lineage()
        return ForecastResponse(
            forecast_id=f"{FORECAST_ID_PREFIX}{uuid.uuid4().hex[:16]}",
            store_id=request.store_id,
            model=resolved.model,
            model_version=resolved.version,
            model_stage=resolved.stage,
            generated_at=utc_now(),
            horizon_days=request.horizon_days,
            confidence_level=request.confidence_level,
            summary=_summarise(bands),
            forecasts=bands,
            metadata=ForecastMetadata(
                history_start_date=request.history_start_date.isoformat(),
                history_end_date=request.history_end_date.isoformat(),
                history_days=series.days,
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                feature_count=bundle.feature_count,
                uncertainty_method=uncertainty.method,
                residual_std=round(uncertainty.residual_std, 4),
                imputed_features=list(policy.imputed),
                run_id=resolved.run_id,
                data_source=lineage.get("data_source"),
                dataset_version=lineage.get("dataset_version"),
            ),
            warnings=warnings,
        )

    def project(
        self,
        *,
        resolved: ResolvedModelVersion,
        history: Sequence[Any],
        store_id: str,
        horizon_days: int,
    ) -> Projection:
        """
        Load the model and run the recursive point forecast.

        The shared core of every forecast in the platform: validate the history
        against the model's own feature contract, decide how to reconstruct the
        inputs that cannot be known for a future date, then step forward.
        """
        if not resolved.run_id:
            raise PredictionFailedError(
                "The selected model version has no training run, so its "
                "artifacts cannot be located.",
                field="model_version",
                details={"model": resolved.model, "version": resolved.version},
            )

        bundle = self.artifacts.load_bundle(resolved.run_id, resolved.model)
        builder = FeatureBuilder(bundle, self.settings)
        builder.assert_supported()

        series = builder.read_history(history)
        warnings = builder.validate_history(series)
        policy = builder.build_policy(series)
        store_code = builder.encode_store(store_id)

        predictions = self._run_recursive(
            builder=builder,
            bundle=bundle,
            sales_history=series.sales,
            store_code=store_code,
            policy=policy,
            start_date=series.dates[-1] + timedelta(days=1),
            horizon_days=horizon_days,
        )
        return Projection(
            predictions=predictions,
            bundle=bundle,
            series=series,
            policy=policy,
            warnings=warnings,
        )

    def _run_recursive(
        self,
        *,
        builder: FeatureBuilder,
        bundle: ModelBundle,
        sales_history: Sequence[float],
        store_code: float,
        policy: Any,
        start_date: date,
        horizon_days: int,
    ) -> List[Tuple[date, float]]:
        """
        Predict one day at a time, feeding each prediction back as history.

        A non-finite or negative prediction is clamped rather than returned:
        sales cannot be negative, and a NaN reaching the response would break
        every consumer downstream.
        """
        series = [float(value) for value in sales_history]
        results: List[Tuple[date, float]] = []

        for step in range(horizon_days):
            target_date = start_date + timedelta(days=step)
            row = builder.build_row(target_date, series, store_code, policy)
            frame = builder.to_frame([row])
            try:
                raw = bundle.model.predict(builder.scale(frame))
            except Exception as exc:  # noqa: BLE001 - reported as a typed error
                raise PredictionFailedError(
                    "The model failed while producing the forecast.",
                    details={
                        "model": bundle.model_name,
                        "forecast_day": target_date.isoformat(),
                        "error": str(exc),
                    },
                ) from exc

            value = _first_scalar(raw)
            if value is None or not math.isfinite(value):
                raise PredictionFailedError(
                    "The model returned a non-numeric prediction.",
                    details={
                        "model": bundle.model_name,
                        "forecast_day": target_date.isoformat(),
                    },
                )
            value = max(0.0, value)
            results.append((target_date, value))
            series.append(value)

        return results

    def _resolve_uncertainty(
        self,
        resolved: ResolvedModelVersion,
        history: Sequence[float],
        predictions: Sequence[Tuple[date, float]],
    ) -> _Uncertainty:
        """
        Choose the residual spread for the interval and record its provenance.

        Bounded relative to the forecast level in both directions so the band
        stays informative whatever the recorded metric happens to be.
        """
        level = _mean([value for _, value in predictions]) or _mean(history) or 1.0
        floor = MIN_RESIDUAL_FRACTION * abs(level)
        ceiling = MAX_RESIDUAL_FRACTION * abs(level)
        warnings: List[str] = []

        rmse = resolved.metrics.get("rmse")
        if rmse is not None and math.isfinite(rmse) and rmse > 0:
            method = UNCERTAINTY_METHOD_RESIDUAL
            residual = float(rmse)
        else:
            method = UNCERTAINTY_METHOD_HISTORY
            residual = _stdev(history)
            warnings.append(HISTORY_VOLATILITY_WARNING)

        bounded = min(max(residual, floor), ceiling)
        if method == UNCERTAINTY_METHOD_RESIDUAL and bounded != residual:
            warnings.append(
                "The model's recorded error was outside the plausible range for "
                "this forecast level, so the interval width was bounded."
            )
        return _Uncertainty(residual_std=bounded, method=method, warnings=warnings)

    @staticmethod
    def _build_bands(
        predictions: Sequence[Tuple[date, float]],
        confidence_level: float,
        uncertainty: _Uncertainty,
    ) -> List[DailyForecast]:
        """
        Attach an interval to each point forecast.

        Widened by ``sqrt(step)``: in a recursive forecast each day inherits the
        error of the days before it, so the variance accumulates roughly
        linearly in the number of steps.
        """
        z = Z_SCORES.get(round(confidence_level, 4), Z_SCORES[0.95])
        bands: List[DailyForecast] = []
        for step, (target_date, value) in enumerate(predictions, start=1):
            half_width = z * uncertainty.residual_std * math.sqrt(step)
            bands.append(
                DailyForecast(
                    date=target_date,
                    predicted_sales=round(value, 2),
                    lower_bound=round(max(0.0, value - half_width), 2),
                    upper_bound=round(value + half_width, 2),
                )
            )
        return bands


def _summarise(bands: Sequence[DailyForecast]) -> ForecastSummary:
    """Horizon totals and the daily average."""
    total = sum(item.predicted_sales for item in bands)
    return ForecastSummary(
        total_forecast_sales=round(total, 2),
        average_daily_sales=round(total / len(bands), 2) if bands else 0.0,
        lower_bound_total=round(sum(item.lower_bound for item in bands), 2),
        upper_bound_total=round(sum(item.upper_bound for item in bands), 2),
    )


def _first_scalar(value: Any) -> Optional[float]:
    """
    Extract a single float from whatever shape an estimator returned.

    Estimators return arrays, nested arrays, or bare numbers depending on
    flavour, so the unwrapping is explicit rather than assumed.
    """
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


def _mean(values: Sequence[float]) -> float:
    """Arithmetic mean, zero for an empty sequence."""
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    """Sample standard deviation, zero when there is nothing to spread."""
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


__all__ = [
    "HISTORY_VOLATILITY_WARNING",
    "Projection",
    "MAX_RESIDUAL_FRACTION",
    "MIN_RESIDUAL_FRACTION",
    "RECURSIVE_WARNING",
    "Z_SCORES",
    "ForecastService",
]
