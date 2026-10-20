"""
Feature schema resolution for a trained model version.

Loads the ``feature_schema.json`` artifact that training writes alongside the
model, through the same MLflow artifact access layer the Streamlit loader uses
(``mlflow.tracking.MlflowClient``). Nothing here registers, logs, or mutates an
MLflow model; this module is strictly read-only.

Artifact access sits behind the ``ArtifactClient`` protocol so the service can be
unit tested without a live MLflow server, and so a future S3/MinIO-direct reader
can be swapped in without touching the capability logic.

Note: the artifact producer does not exist yet -- training does not currently
write ``feature_schema.json``. Until a later task adds the writer, this service
raises FEATURE_SCHEMA_MISSING, which is exactly the contract the frontend needs
to degrade gracefully.
"""

from __future__ import annotations

import json
import logging
import os
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from pydantic import ValidationError

from api.schemas.scenario import DEFAULT_MODEL_VERSION
from api.schemas.scenario_capabilities import FeatureSchema, FeatureSchemaErrorResponse

logger = logging.getLogger(__name__)

DEFAULT_FEATURE_SCHEMA_ARTIFACT = "feature_schema.json"
FEATURE_SCHEMA_ARTIFACT_ENV_VAR = "FEATURE_SCHEMA_ARTIFACT_PATH"
MLFLOW_EXPERIMENT_ENV_VAR = "MLFLOW_EXPERIMENT_NAME"
MLFLOW_REGISTERED_MODEL_ENV_VAR = "MLFLOW_REGISTERED_MODEL_NAME"

DEFAULT_EXPERIMENT_NAME = "sales_forecasting"
DEFAULT_REGISTERED_MODEL_NAME = "sales_forecast_models_ensemble"

LATEST_VERSION_ALIASES = ("latest", "", "none", "production")
_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


class FeatureSchemaErrorCode(str, Enum):
    """Machine-readable codes for feature-schema resolution failures."""

    FEATURE_SCHEMA_MISSING = "FEATURE_SCHEMA_MISSING"
    FEATURE_SCHEMA_INVALID = "FEATURE_SCHEMA_INVALID"
    FEATURE_SCHEMA_UNREADABLE = "FEATURE_SCHEMA_UNREADABLE"
    MODEL_VERSION_NOT_FOUND = "MODEL_VERSION_NOT_FOUND"


FEATURE_SCHEMA_MISSING_MESSAGE = "Feature schema is not available for this model version."


class FeatureSchemaError(Exception):
    """
    A model version's feature schema could not be resolved.

    Mirrors the ``error_code`` / ``message`` / ``field`` / ``details`` shape of
    ``ScenarioValidationError`` so a route layer can handle both the same way.
    """

    def __init__(
        self,
        error_code: FeatureSchemaErrorCode | str,
        message: str,
        field: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_code: str = (
            error_code.value if isinstance(error_code, FeatureSchemaErrorCode) else error_code
        )
        self.message = message
        self.field = field
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

    def to_response(self) -> FeatureSchemaErrorResponse:
        """Schema form, ready to serialise as an error body."""
        return FeatureSchemaErrorResponse(**self.to_dict())

    def __repr__(self) -> str:
        return (
            f"FeatureSchemaError(error_code={self.error_code!r}, "
            f"field={self.field!r}, message={self.message!r})"
        )


@runtime_checkable
class ArtifactClient(Protocol):
    """Read-only artifact access needed to resolve a feature schema."""

    def resolve_run_id(self, model_version: str) -> str:
        """Return the MLflow run id backing ``model_version``."""
        ...

    def download_artifact(self, run_id: str, artifact_path: str) -> Optional[str]:
        """Return a local path for the artifact, or None when it does not exist."""
        ...


class MlflowArtifactClient:
    """
    ``ArtifactClient`` backed by MLflow.

    ``mlflow`` is imported lazily so importing this module (and unit testing the
    services with a fake client) never requires the MLflow package or a server.

    ``model_version`` resolution, in order:
      1. ``latest`` / ``production`` / empty -> newest FINISHED run in the experiment
      2. a 32-character hex string          -> that run id directly
      3. anything else                      -> registry version of the configured
                                               registered model
    """

    def __init__(
        self,
        tracking_uri: Optional[str] = None,
        experiment_name: Optional[str] = None,
        registered_model_name: Optional[str] = None,
    ) -> None:
        self.tracking_uri = tracking_uri or os.getenv("MLFLOW_TRACKING_URI")
        self.experiment_name = experiment_name or os.getenv(
            MLFLOW_EXPERIMENT_ENV_VAR, DEFAULT_EXPERIMENT_NAME
        )
        self.registered_model_name = registered_model_name or os.getenv(
            MLFLOW_REGISTERED_MODEL_ENV_VAR, DEFAULT_REGISTERED_MODEL_NAME
        )

    def _client(self):
        """Build an MlflowClient, honouring the configured tracking URI."""
        import mlflow  # imported lazily; see class docstring

        if self.tracking_uri:
            mlflow.set_tracking_uri(self.tracking_uri)
        return mlflow.tracking.MlflowClient()

    def resolve_run_id(self, model_version: str) -> str:
        """Resolve a model version reference to a concrete run id."""
        version = (model_version or "").strip()

        if version.lower() in LATEST_VERSION_ALIASES:
            return self._latest_finished_run_id()
        if _RUN_ID_PATTERN.match(version):
            return version
        return self._registry_run_id(version)

    def _latest_finished_run_id(self) -> str:
        """Newest FINISHED run of the configured experiment."""
        import mlflow  # imported lazily

        if self.tracking_uri:
            mlflow.set_tracking_uri(self.tracking_uri)

        experiment = mlflow.get_experiment_by_name(self.experiment_name)
        if experiment is None:
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.MODEL_VERSION_NOT_FOUND,
                f"MLflow experiment '{self.experiment_name}' was not found.",
                field="model_version",
                details={"experiment_name": self.experiment_name},
            )

        runs = mlflow.search_runs(
            experiment_ids=[experiment.experiment_id],
            filter_string="status = 'FINISHED'",
            order_by=["start_time DESC"],
            max_results=1,
        )
        if len(runs) == 0:
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.MODEL_VERSION_NOT_FOUND,
                "No finished training runs were found.",
                field="model_version",
                details={"experiment_name": self.experiment_name},
            )
        return str(runs.iloc[0]["run_id"])

    def _registry_run_id(self, version: str) -> str:
        """Run id behind a registered model version."""
        try:
            model_version = self._client().get_model_version(
                name=self.registered_model_name, version=version
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.MODEL_VERSION_NOT_FOUND,
                f"Model version '{version}' was not found in the registry.",
                field="model_version",
                details={
                    "model_version": version,
                    "registered_model_name": self.registered_model_name,
                    "error": str(exc),
                },
            ) from exc
        return str(model_version.run_id)

    def download_artifact(self, run_id: str, artifact_path: str) -> Optional[str]:
        """Download one artifact, returning None when the run does not contain it."""
        client = self._client()

        if not self._artifact_exists(client, run_id, artifact_path):
            return None

        try:
            return client.download_artifacts(run_id, artifact_path)
        except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.FEATURE_SCHEMA_UNREADABLE,
                f"Could not download artifact '{artifact_path}'.",
                field="model_version",
                details={"run_id": run_id, "artifact_path": artifact_path, "error": str(exc)},
            ) from exc

    @staticmethod
    def _artifact_exists(client: Any, run_id: str, artifact_path: str) -> bool:
        """
        Check presence via ``list_artifacts`` so a genuinely missing schema is
        distinguishable from a transport failure.
        """
        parent = os.path.dirname(artifact_path)
        try:
            entries = client.list_artifacts(run_id, parent) if parent else client.list_artifacts(run_id)
        except Exception as exc:  # noqa: BLE001 - treated as unreadable, not missing
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.FEATURE_SCHEMA_UNREADABLE,
                f"Could not list artifacts for run '{run_id}'.",
                field="model_version",
                details={"run_id": run_id, "artifact_path": artifact_path, "error": str(exc)},
            ) from exc
        return any(entry.path == artifact_path for entry in entries)


class FeatureSchemaService:
    """
    Resolves and caches the feature schema for a model version.

    Successful loads are cached per model version; failures are never cached, so
    a schema that appears after the next training run is picked up immediately.
    """

    def __init__(
        self,
        artifact_client: Optional[ArtifactClient] = None,
        artifact_path: Optional[str] = None,
        cache_enabled: bool = True,
    ) -> None:
        self._artifact_client = artifact_client
        self.artifact_path = artifact_path or os.getenv(
            FEATURE_SCHEMA_ARTIFACT_ENV_VAR, DEFAULT_FEATURE_SCHEMA_ARTIFACT
        )
        self.cache_enabled = cache_enabled
        self._cache: Dict[str, FeatureSchema] = {}

    @property
    def artifact_client(self) -> ArtifactClient:
        """The artifact client, defaulting to MLflow when none was injected."""
        if self._artifact_client is None:
            self._artifact_client = MlflowArtifactClient()
        return self._artifact_client

    def clear_cache(self) -> None:
        """Drop every cached schema."""
        self._cache.clear()

    def get_feature_schema(self, model_version: str = DEFAULT_MODEL_VERSION) -> FeatureSchema:
        """
        Load the feature schema for ``model_version``.

        Raises ``FeatureSchemaError`` with FEATURE_SCHEMA_MISSING when the
        artifact does not exist, FEATURE_SCHEMA_INVALID when it exists but does
        not match the contract, and MODEL_VERSION_NOT_FOUND when the version
        cannot be resolved to a run.
        """
        version = (model_version or DEFAULT_MODEL_VERSION).strip() or DEFAULT_MODEL_VERSION
        cache_key = version.lower()

        if self.cache_enabled and cache_key in self._cache:
            return self._cache[cache_key]

        run_id = self.artifact_client.resolve_run_id(version)
        local_path = self.artifact_client.download_artifact(run_id, self.artifact_path)

        if not local_path or not os.path.isfile(local_path):
            raise self._missing_error(version, run_id)

        payload = self._read_json(version, run_id, local_path)
        schema = self._parse_schema(version, run_id, payload)

        if self.cache_enabled:
            self._cache[cache_key] = schema
        return schema

    def _missing_error(self, model_version: str, run_id: str) -> FeatureSchemaError:
        """The documented FEATURE_SCHEMA_MISSING failure."""
        logger.warning(
            "Feature schema artifact '%s' not found for model version %s (run %s)",
            self.artifact_path,
            model_version,
            run_id,
        )
        return FeatureSchemaError(
            FeatureSchemaErrorCode.FEATURE_SCHEMA_MISSING,
            FEATURE_SCHEMA_MISSING_MESSAGE,
            field="model_version",
            details={
                "model_version": model_version,
                "run_id": run_id,
                "artifact_path": self.artifact_path,
            },
        )

    def _read_json(self, model_version: str, run_id: str, local_path: str) -> Any:
        """Read the artifact, converting read/parse problems into typed errors."""
        try:
            with open(local_path, encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError as exc:
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.FEATURE_SCHEMA_INVALID,
                "Feature schema artifact is not valid JSON.",
                field="model_version",
                details={
                    "model_version": model_version,
                    "run_id": run_id,
                    "artifact_path": self.artifact_path,
                    "error": str(exc),
                },
            ) from exc
        except OSError as exc:
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.FEATURE_SCHEMA_UNREADABLE,
                "Feature schema artifact could not be read.",
                field="model_version",
                details={
                    "model_version": model_version,
                    "run_id": run_id,
                    "artifact_path": self.artifact_path,
                    "error": str(exc),
                },
            ) from exc

    def _parse_schema(self, model_version: str, run_id: str, payload: Any) -> FeatureSchema:
        """Validate the payload against the FeatureSchema contract."""
        if not isinstance(payload, dict):
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.FEATURE_SCHEMA_INVALID,
                "Feature schema artifact must be a JSON object.",
                field="model_version",
                details={
                    "model_version": model_version,
                    "run_id": run_id,
                    "received_type": type(payload).__name__,
                },
            )

        data = dict(payload)
        data.setdefault("model_version", model_version)
        data.setdefault("run_id", run_id)

        try:
            return FeatureSchema(**data)
        except ValidationError as exc:
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.FEATURE_SCHEMA_INVALID,
                "Feature schema artifact does not match the expected contract.",
                field="model_version",
                details={
                    "model_version": model_version,
                    "run_id": run_id,
                    "artifact_path": self.artifact_path,
                    "validation_errors": exc.errors(include_url=False),
                },
            ) from exc

    def list_features(self, model_version: str = DEFAULT_MODEL_VERSION) -> List[str]:
        """Feature names for ``model_version``."""
        return list(self.get_feature_schema(model_version).features)
