"""
Read-only access to the MLflow model registry.

Answers two questions for the serving layer: *which* model version should serve
a request, and *what is known about it*. Nothing here registers, promotes, or
mutates anything -- the training DAG owns that.

Why this does not call ``search_registered_models()``
-----------------------------------------------------
The deployed tracking server is MLflow 2.9.2 while the installed client may be
3.x. Under that split ``search_registered_models()`` returns an empty list and
``list_artifacts()`` on a sub-path raises 404 against the removed
``/logged-models`` endpoints -- both silently, which would make the API report
"no models" while three Production versions exist. So enumeration is driven by
the configured model names and uses only the calls verified to work across the
split: ``search_model_versions(filter_string=...)``, ``get_model_version``, and
``get_run``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from api.config import Settings, get_settings
from api.core.errors import (
    MLflowUnavailableError,
    ModelNotFoundError,
    ModelNotInProductionError,
)

logger = logging.getLogger(__name__)

PRODUCTION_STAGE = "Production"
LATEST_ALIASES = ("latest", "production", "")

#: Metric suffixes training logs per model, as ``<model>_<suffix>``.
METRIC_SUFFIXES = ("rmse", "mae", "r2", "mape", "wape", "bias")

#: Lineage params worth surfacing, mapped to the response field name.
LINEAGE_PARAMS = {
    "data.source": "data_source",
    "data.version": "dataset_version",
    "data.hash": "dataset_hash",
    "data.rows": "dataset_rows",
    "data.date_min": "dataset_date_min",
    "data.date_max": "dataset_date_max",
    "data.stores": "dataset_stores",
    "data.seed": "dataset_seed",
}


@dataclass(frozen=True)
class ResolvedModelVersion:
    """A concrete registry version, with what the run recorded about it."""

    model: str
    registered_model_name: str
    version: str
    stage: str
    run_id: Optional[str] = None
    created_at_ms: Optional[int] = None
    metrics: Dict[str, float] = field(default_factory=dict)
    params: Dict[str, str] = field(default_factory=dict)

    @property
    def is_production(self) -> bool:
        """Whether this version is the one serving production traffic."""
        return self.stage == PRODUCTION_STAGE

    @property
    def training_date(self) -> Optional[str]:
        """Version creation time as an ISO-8601 string."""
        if not self.created_at_ms:
            return None
        try:
            return (
                datetime.fromtimestamp(self.created_at_ms / 1000.0, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            return None

    def lineage(self) -> Dict[str, Any]:
        """Dataset lineage the training run logged, under stable field names."""
        return {
            alias: self.params[key]
            for key, alias in LINEAGE_PARAMS.items()
            if key in self.params
        }


class ModelRegistryService:
    """
    Registry lookups for the serving layer.

    ``client_factory`` is injectable so the whole service can be unit tested
    against a fake registry without MLflow installed or a server running.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client_factory = client_factory
        self._client: Any = None
        self._run_cache: Dict[str, Tuple[Dict[str, float], Dict[str, str]]] = {}

    # -- client ------------------------------------------------------------

    def _build_client(self) -> Any:
        """Create an MlflowClient, or raise a typed unavailability error."""
        if self._client_factory is not None:
            return self._client_factory()
        try:
            import mlflow

            if self.settings.mlflow_tracking_uri:
                mlflow.set_tracking_uri(self.settings.mlflow_tracking_uri)
            return mlflow.tracking.MlflowClient()
        except Exception as exc:  # noqa: BLE001 - reported as a typed error
            raise MLflowUnavailableError(
                "The model tracking service is currently unavailable.",
                field="model_version",
                details={"error": str(exc)},
            ) from exc

    @property
    def client(self) -> Any:
        """Lazily built MLflow client, reused for the service's lifetime."""
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # -- queries -----------------------------------------------------------

    def _search_versions(self, registered_model_name: str) -> List[Any]:
        """
        All versions of one registered model.

        A missing registered model is not an outage: MLflow raises for an
        unknown name, and the honest answer is an empty version list.
        """
        try:
            return list(
                self.client.search_model_versions(f"name='{registered_model_name}'")
            )
        except Exception as exc:  # noqa: BLE001 - distinguish absent from down
            message = str(exc).lower()
            if "not exist" in message or "not found" in message:
                logger.info(
                    "Registered model %s does not exist yet", registered_model_name
                )
                return []
            raise MLflowUnavailableError(
                "Could not query the model registry.",
                field="model_version",
                details={"error": str(exc)},
            ) from exc

    def _run_data(self, run_id: Optional[str]) -> Tuple[Dict[str, float], Dict[str, str]]:
        """
        Metrics and params of a run, cached per run id.

        Best-effort: a run whose metadata cannot be read still yields a usable
        model version, just without metrics.
        """
        if not run_id:
            return {}, {}
        if run_id in self._run_cache:
            return self._run_cache[run_id]
        try:
            data = self.client.get_run(run_id).data
            metrics = {str(k): float(v) for k, v in dict(data.metrics).items()}
            params = {str(k): str(v) for k, v in dict(data.params).items()}
        except Exception as exc:  # noqa: BLE001 - metrics are not essential
            logger.warning("Could not read run %s: %s", run_id, exc)
            metrics, params = {}, {}
        self._run_cache[run_id] = (metrics, params)
        return metrics, params

    @staticmethod
    def _model_metrics(model: str, run_metrics: Dict[str, float]) -> Dict[str, float]:
        """Pull out the ``<model>_<metric>`` entries for one model."""
        return {
            suffix: run_metrics[f"{model}_{suffix}"]
            for suffix in METRIC_SUFFIXES
            if f"{model}_{suffix}" in run_metrics
        }

    def _to_resolved(self, model: str, version: Any) -> ResolvedModelVersion:
        """Convert an MLflow ``ModelVersion`` into the service's own type."""
        run_id = getattr(version, "run_id", None)
        run_metrics, run_params = self._run_data(run_id)
        return ResolvedModelVersion(
            model=model,
            registered_model_name=self.settings.registered_model_name(model),
            version=str(getattr(version, "version", "")),
            stage=getattr(version, "current_stage", None) or "None",
            run_id=run_id,
            created_at_ms=getattr(version, "creation_timestamp", None),
            metrics=self._model_metrics(model, run_metrics),
            params=run_params,
        )

    # -- public API --------------------------------------------------------

    def known_models(self) -> Tuple[str, ...]:
        """Short model names this deployment is configured to serve."""
        return self.settings.model_names

    def list_versions(self, model: str) -> List[ResolvedModelVersion]:
        """Every version of one model, newest first."""
        name = self.settings.registered_model_name(model)
        versions = self._search_versions(name)
        resolved = [self._to_resolved(model, version) for version in versions]
        resolved.sort(key=lambda item: _version_sort_key(item.version), reverse=True)
        return resolved

    def list_serving_versions(self) -> Tuple[List[ResolvedModelVersion], List[str]]:
        """
        The version that would serve each configured model, plus any warnings.

        "Would serve" means the newest Production version, falling back to the
        newest version of any stage when none is promoted -- the fallback is
        reported as a warning rather than passed off as production-ready.
        """
        serving: List[ResolvedModelVersion] = []
        warnings: List[str] = []

        for model in self.known_models():
            versions = self.list_versions(model)
            if not versions:
                warnings.append(f"No registered versions found for '{model}'.")
                continue
            production = [v for v in versions if v.is_production]
            if production:
                serving.append(production[0])
            else:
                serving.append(versions[0])
                warnings.append(
                    f"'{model}' has no Production version; showing version "
                    f"{versions[0].version} (stage {versions[0].stage})."
                )

        if not serving:
            warnings.append(
                "The model registry has no versions for any configured model. "
                "Run the training DAG to train and promote a model."
            )
        return serving, warnings

    def resolve(self, model: str, model_version: str = "latest") -> ResolvedModelVersion:
        """
        Resolve a (model, version) reference to the version that will serve.

        Raises ``ModelNotFoundError`` for an unknown model or version, and
        ``ModelNotInProductionError`` when a non-Production version is requested
        while the deployment requires Production (the default).
        """
        model = (model or self.settings.default_model).strip().lower()
        if model not in self.known_models():
            raise ModelNotFoundError(
                f"Unknown model '{model}'.",
                field="model",
                details={"available_models": list(self.known_models())},
            )

        requested = (model_version or "latest").strip()
        versions = self.list_versions(model)
        if not versions:
            raise ModelNotFoundError(
                f"No registered versions exist for '{model}'. "
                "Run the training DAG to train and register a model.",
                field="model_version",
                details={"model": model},
            )

        if requested.lower() in LATEST_ALIASES:
            production = [v for v in versions if v.is_production]
            if production:
                return production[0]
            if self.settings.require_production_stage:
                raise ModelNotInProductionError(
                    f"No Production version of '{model}' is available.",
                    field="model_version",
                    details={
                        "model": model,
                        "available_versions": [v.version for v in versions],
                        "stages": sorted({v.stage for v in versions}),
                    },
                )
            return versions[0]

        matches = [v for v in versions if v.version == requested]
        if not matches:
            raise ModelNotFoundError(
                f"Model version '{requested}' was not found for '{model}'.",
                field="model_version",
                details={
                    "model": model,
                    "available_versions": [v.version for v in versions],
                },
            )

        chosen = matches[0]
        if self.settings.require_production_stage and not chosen.is_production:
            raise ModelNotInProductionError(
                f"Version {chosen.version} of '{model}' is in stage "
                f"'{chosen.stage}', not Production.",
                field="model_version",
                details={"model": model, "version": chosen.version, "stage": chosen.stage},
            )
        return chosen

    def production_summary(self) -> Optional[Dict[str, Any]]:
        """
        Compact description of the default model's Production version.

        Used by ``/health``; returns ``None`` rather than raising, because a
        health probe reports state instead of failing on it.
        """
        try:
            resolved = self.resolve(self.settings.default_model, "latest")
        except Exception as exc:  # noqa: BLE001 - health must not propagate
            logger.info("No production model available for health report: %s", exc)
            return None
        return {
            "model": resolved.model,
            "registered_model_name": resolved.registered_model_name,
            "version": resolved.version,
            "stage": resolved.stage,
            "run_id": resolved.run_id,
            "training_date": resolved.training_date,
            "metrics": resolved.metrics,
        }


def _version_sort_key(version: str) -> Tuple[int, int, str]:
    """
    Sort versions numerically when possible.

    Registry versions are integers, but sorting them as strings would place
    version 10 before version 9, so numeric ordering is explicit.
    """
    try:
        return (1, int(version), "")
    except (TypeError, ValueError):
        return (0, 0, str(version))


__all__ = [
    "LATEST_ALIASES",
    "METRIC_SUFFIXES",
    "PRODUCTION_STAGE",
    "ModelRegistryService",
    "ResolvedModelVersion",
]
