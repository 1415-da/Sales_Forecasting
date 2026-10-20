"""
Dataset lineage for scenario responses.

Answers "which data was the model behind this forecast trained on?" using what
the training pipeline already records -- never hardcoded values:

1. **Training run metadata** (preferred): the MLflow params that
   ``include/utils/dataset_lineage.DatasetLineage.to_mlflow_params`` logs on
   every training run (``data.source``, ``data.version``, ``data.hash``, ...).
2. **Dataset registry** (fallback): the ``dataset_manifest.json`` that each data
   provider writes beside its parquet, carrying the same lineage under slightly
   different key names.

Lineage is advisory, never load-bearing: a scenario must not fail because its
lineage could not be read. When nothing can be resolved the result carries
``lineage_status="unavailable"`` plus the documented warning, and the caller
proceeds.

Known gap: training does not currently log ``processed_data_path`` or
``ingestion_run_id``, so a lineage read against today's runs resolves 7 of 9
fields and reports ``partial``. Closing that needs a change to the training DAG,
which is out of scope here.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

LINEAGE_UNAVAILABLE_WARNING = (
    "Dataset lineage metadata is unavailable for this model version."
)
LINEAGE_PARTIAL_WARNING_TEMPLATE = (
    "Dataset lineage metadata is incomplete for this model version; missing: {fields}."
)

DATASET_REGISTRY_PATH_ENV_VAR = "DATASET_REGISTRY_PATH"
MANIFEST_FILENAME = "dataset_manifest.json"

#: Fields that must resolve for lineage to count as fully available.
CORE_LINEAGE_FIELDS = ("data_source", "dataset_version", "dataset_hash")

#: MLflow param keys, as logged by DatasetLineage.to_mlflow_params().
RUN_PARAM_KEYS: Dict[str, str] = {
    "data_source": "data.source",
    "dataset_version": "data.version",
    "dataset_hash": "data.hash",
    "raw_data_path": "data.raw_path",
    "processed_data_path": "data.processed_path",
    "train_start_date": "data.date_min",
    "train_end_date": "data.date_max",
    "feature_schema_version": "data.schema_version",
    "ingestion_run_id": "data.ingestion_run_id",
}

#: dataset_manifest.json lineage keys, as written by the data providers.
MANIFEST_KEYS: Dict[str, str] = {
    "data_source": "data_source",
    "dataset_version": "dataset_version",
    "dataset_hash": "dataset_hash",
    "raw_data_path": "raw_path",
    "processed_data_path": "processed_path",
    "train_start_date": "date_range_start",
    "train_end_date": "date_range_end",
    "feature_schema_version": "schema_version",
    "ingestion_run_id": "ingestion_run_id",
}

DATE_FIELDS = ("train_start_date", "train_end_date")


class DatasetLineageStatus(str, Enum):
    """How much lineage could be resolved."""

    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class LineageSource(str, Enum):
    """Where the lineage was read from."""

    TRAINING_RUN = "training_run"
    DATASET_REGISTRY = "dataset_registry"
    TRAINING_RUN_AND_REGISTRY = "training_run_and_registry"
    NONE = "none"


class ScenarioDatasetLineage(BaseModel):
    """Lineage of the dataset a model version was trained on."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_version: Optional[str] = None
    data_source: Optional[str] = None
    dataset_version: Optional[str] = None
    dataset_hash: Optional[str] = None
    raw_data_path: Optional[str] = None
    processed_data_path: Optional[str] = None
    train_start_date: Optional[date] = None
    train_end_date: Optional[date] = None
    feature_schema_version: Optional[str] = None
    ingestion_run_id: Optional[str] = None

    lineage_status: DatasetLineageStatus = DatasetLineageStatus.UNAVAILABLE
    source_of_record: LineageSource = LineageSource.NONE
    missing_fields: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_available(self) -> bool:
        """Whether any lineage at all was resolved."""
        return self.lineage_status is not DatasetLineageStatus.UNAVAILABLE


@runtime_checkable
class TrainingRunMetadataSource(Protocol):
    """Reads the lineage params a training run logged."""

    def get_run_params(self, model_version: str) -> Dict[str, str]:
        """Return the run's params, or an empty mapping when unavailable."""
        ...


@runtime_checkable
class DatasetRegistry(Protocol):
    """Reads dataset manifests written at ingestion time."""

    def get_manifest(self, raw_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the manifest's lineage block, or None."""
        ...


class MlflowRunMetadataSource:
    """
    ``TrainingRunMetadataSource`` backed by MLflow.

    Reuses ``MlflowArtifactClient`` for version-to-run resolution so this
    service and the feature-schema service agree on what ``latest`` means.
    ``mlflow`` is imported lazily, keeping this module importable without it.
    """

    def __init__(self, artifact_client: Optional[Any] = None) -> None:
        self._artifact_client = artifact_client

    @property
    def artifact_client(self) -> Any:
        """Artifact client, defaulting to the MLflow-backed one."""
        if self._artifact_client is None:
            from api.services.feature_schema_service import MlflowArtifactClient

            self._artifact_client = MlflowArtifactClient()
        return self._artifact_client

    def get_run_params(self, model_version: str) -> Dict[str, str]:
        """Read one run's params, returning {} rather than raising."""
        try:
            run_id = self.artifact_client.resolve_run_id(model_version)
        except Exception as exc:  # noqa: BLE001 - lineage is advisory
            logger.info("Could not resolve run for model version %s: %s", model_version, exc)
            return {}

        try:
            import mlflow

            client = mlflow.tracking.MlflowClient()
            return dict(client.get_run(run_id).data.params)
        except Exception as exc:  # noqa: BLE001 - lineage is advisory
            logger.info("Could not read lineage params for run %s: %s", run_id, exc)
            return {}


class ManifestDatasetRegistry:
    """
    ``DatasetRegistry`` backed by ``dataset_manifest.json`` on disk.

    The data providers already write this file beside the parquet they
    materialise, which makes it the project's de-facto dataset registry.
    """

    def __init__(self, base_path: Optional[str] = None) -> None:
        self.base_path = base_path or os.getenv(DATASET_REGISTRY_PATH_ENV_VAR)

    def get_manifest(self, raw_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Read the lineage block from the first manifest that resolves."""
        for candidate in (raw_path, self.base_path):
            if not candidate:
                continue
            path = (
                candidate
                if candidate.endswith(".json")
                else os.path.join(candidate, MANIFEST_FILENAME)
            )
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    manifest = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                logger.info("Could not read dataset manifest at %s: %s", path, exc)
                continue
            lineage = manifest.get("lineage")
            if isinstance(lineage, dict):
                return lineage
            if isinstance(manifest, dict):
                return manifest
        return None


class DatasetLineageService:
    """
    Resolves dataset lineage for a model version and attaches it to responses.

    Results are cached per model version: lineage is immutable for a given
    trained model, and resolving it costs an MLflow round trip.
    """

    def __init__(
        self,
        run_metadata_source: Optional[TrainingRunMetadataSource] = None,
        dataset_registry: Optional[DatasetRegistry] = None,
        cache_enabled: bool = True,
    ) -> None:
        self._run_metadata_source = run_metadata_source
        self._dataset_registry = dataset_registry
        self.cache_enabled = cache_enabled
        self._cache: Dict[str, ScenarioDatasetLineage] = {}

    @property
    def run_metadata_source(self) -> TrainingRunMetadataSource:
        """Training run reader, defaulting to MLflow."""
        if self._run_metadata_source is None:
            self._run_metadata_source = MlflowRunMetadataSource()
        return self._run_metadata_source

    @property
    def dataset_registry(self) -> DatasetRegistry:
        """Dataset registry, defaulting to on-disk manifests."""
        if self._dataset_registry is None:
            self._dataset_registry = ManifestDatasetRegistry()
        return self._dataset_registry

    def clear_cache(self) -> None:
        """Drop cached lineage."""
        self._cache.clear()

    def get_lineage(self, model_version: str) -> ScenarioDatasetLineage:
        """
        Resolve lineage for a model version.

        Never raises: an unreadable source yields ``unavailable`` lineage with
        the documented warning, because losing lineage must not lose a forecast.
        """
        cache_key = (model_version or "latest").strip().lower()
        if self.cache_enabled and cache_key in self._cache:
            return self._cache[cache_key]

        lineage = self._resolve(model_version)
        if self.cache_enabled and lineage.is_available:
            # Only successful reads are cached, so lineage appears as soon as
            # the training side starts recording it.
            self._cache[cache_key] = lineage
        return lineage

    def attach_lineage(self, response: Any, model_version: Optional[str] = None) -> Any:
        """
        Return a copy of a comparison response with lineage attached.

        Lineage warnings are appended to the response's own warnings, so a
        caller reading only ``warnings`` still learns that lineage is missing.
        """
        version = model_version or getattr(response, "model_version", None) or "latest"
        lineage = self.get_lineage(version)

        existing = list(getattr(response, "warnings", []) or [])
        merged = existing + [item for item in lineage.warnings if item not in existing]

        try:
            return response.model_copy(
                update={
                    "dataset_lineage": lineage.model_dump(mode="json"),
                    "warnings": merged,
                }
            )
        except Exception as exc:  # noqa: BLE001 - lineage must not break a response
            logger.warning("Could not attach dataset lineage to response: %s", exc)
            return response

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def _resolve(self, model_version: str) -> ScenarioDatasetLineage:
        """Merge run params and the registry manifest into one lineage record."""
        values: Dict[str, Any] = {}
        used_run = False
        used_registry = False

        params = self._safe_run_params(model_version)
        if params:
            for field, key in RUN_PARAM_KEYS.items():
                raw = params.get(key)
                if raw not in (None, ""):
                    values[field] = raw
                    used_run = True

        manifest = self._safe_manifest(values.get("raw_data_path"))
        if manifest:
            for field, key in MANIFEST_KEYS.items():
                if field in values:
                    continue
                raw = manifest.get(key)
                if raw not in (None, ""):
                    values[field] = raw
                    used_registry = True

        for field in DATE_FIELDS:
            if field in values:
                parsed = _parse_date(values[field])
                if parsed is None:
                    values.pop(field)
                else:
                    values[field] = parsed

        missing = [field for field in RUN_PARAM_KEYS if field not in values]
        status = self._status(values, missing)
        warnings = self._warnings(status, missing)

        return ScenarioDatasetLineage(
            model_version=model_version,
            lineage_status=status,
            source_of_record=self._source(used_run, used_registry),
            missing_fields=missing,
            warnings=warnings,
            **values,
        )

    def _safe_run_params(self, model_version: str) -> Dict[str, str]:
        """Run params, tolerating any failure."""
        try:
            return self.run_metadata_source.get_run_params(model_version) or {}
        except Exception as exc:  # noqa: BLE001 - lineage is advisory
            logger.info("Training run lineage unavailable for %s: %s", model_version, exc)
            return {}

    def _safe_manifest(self, raw_path: Optional[str]) -> Optional[Dict[str, Any]]:
        """Registry manifest, tolerating any failure."""
        try:
            return self.dataset_registry.get_manifest(raw_path)
        except Exception as exc:  # noqa: BLE001 - lineage is advisory
            logger.info("Dataset registry lineage unavailable: %s", exc)
            return None

    @staticmethod
    def _status(values: Dict[str, Any], missing: List[str]) -> DatasetLineageStatus:
        """
        Classify completeness.

        ``available`` needs the core identity fields -- source, version, and
        content hash -- since those are what make a forecast reproducible. Other
        gaps downgrade to ``partial`` rather than discarding usable lineage.
        """
        if not values:
            return DatasetLineageStatus.UNAVAILABLE
        if any(field not in values for field in CORE_LINEAGE_FIELDS):
            return DatasetLineageStatus.PARTIAL
        return (
            DatasetLineageStatus.AVAILABLE if not missing else DatasetLineageStatus.PARTIAL
        )

    @staticmethod
    def _warnings(status: DatasetLineageStatus, missing: List[str]) -> List[str]:
        """The caller-facing warnings for a lineage status."""
        if status is DatasetLineageStatus.UNAVAILABLE:
            return [LINEAGE_UNAVAILABLE_WARNING]
        if status is DatasetLineageStatus.PARTIAL:
            return [
                LINEAGE_PARTIAL_WARNING_TEMPLATE.format(fields=", ".join(missing))
            ]
        return []

    @staticmethod
    def _source(used_run: bool, used_registry: bool) -> LineageSource:
        """Which source(s) contributed."""
        if used_run and used_registry:
            return LineageSource.TRAINING_RUN_AND_REGISTRY
        if used_run:
            return LineageSource.TRAINING_RUN
        if used_registry:
            return LineageSource.DATASET_REGISTRY
        return LineageSource.NONE


def _parse_date(value: Any) -> Optional[date]:
    """Parse a date from a param string, returning None when unusable."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        logger.info("Could not parse lineage date %r", value)
        return None
