"""
Training dataset upload sessions and immutable versions.

The connected path for every training upload:

    upload (streamed to staging, originals stored write-once, hashed)
      -> process   [background job for large uploads, inline for small ones]
           package inspection (ZIP limits, content checks)
           adapter detection (evidence, required confirmations)
           normalization by the confirmed adapter
           strict store_day_v1 validation + readiness
           append combination (when chosen)
           normalized Parquet + retained context written (content-addressed)
      -> configure (adapter / mapping / policies; bumps mapping_version and
                    invalidates the previous validation)
      -> seal      (only a validated session at the expected mapping version,
                    with required warnings acknowledged; writes the manifest
                    and makes the version immutable)

Original uploads are never modified; normalized output is stored separately.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.core.errors import (
    DatasetInvalidError,
    DatasetNotFoundError,
    DatasetNotReadyError,
    IdempotencyKeyConflictError,
    ScenarioValidationError,
    StorageUnavailableError,
)
from api.db.base import utcnow
from api.db.models.training import DatasetMode, DatasetStatus, TrainingDataset
from api.db.models.user import User
from data_ingestion import pipeline
from data_ingestion.limits import UploadLimits
from data_ingestion.package_inspector import StagedFile, inspect_package
from training import contract, storage

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,79}$")
UPLOAD_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
MANIFEST_VERSION = 2

#: Configuration keys a user may set. Anything else is refused, so nothing
#: unvalidated can reach an adapter.
CONFIG_KEYS = {
    "adapter_id", "currency", "missing_dates", "closure", "confirm_source",
    "file", "date", "store_id", "sales", "open", "is_closed", "date_format",
    "target_type", "frequency", "source_grain", "single_store_id", "confirm_single_store",
}
CONTENT_TYPES = {"csv": "text/csv", "parquet": "application/vnd.apache.parquet", "zip": "application/zip"}

#: Optional hook computing deployment compatibility for a validated dataset.
DeploymentCheck = Callable[[List[str], str], Dict[str, Any]]


def _safe_filename(filename: Optional[str]) -> Optional[str]:
    """An upload's name for display: basename only, printable, bounded."""
    if not filename:
        return None
    base = re.split(r"[\\/]", filename)[-1]
    cleaned = "".join(ch for ch in base if ch.isprintable())[:255]
    return cleaned or None


def _extension(name: str) -> Optional[str]:
    lowered = name.lower()
    for ext in ("csv", "parquet", "zip"):
        if lowered.endswith(f".{ext}"):
            return ext
    if lowered.endswith(".pq"):
        return "parquet"
    return None


def clean_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = dict(config or {})
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        raise ScenarioValidationError(f"Unknown configuration keys: {', '.join(unknown)}.", field="config")
    cleaned: Dict[str, Any] = {}
    for key, value in config.items():
        if value is None or value == "":
            continue
        if key == "confirm_single_store":
            cleaned[key] = value is True
        elif isinstance(value, (str, int, float, bool)):
            cleaned[key] = value.strip() if isinstance(value, str) else value
        else:
            raise ScenarioValidationError(f"{key} must be a simple value.", field=key)
    if "currency" in cleaned:
        cleaned["currency"] = str(cleaned["currency"]).upper()
    return cleaned


class TrainingDatasetService:
    def __init__(
        self,
        session: Session,
        store: Optional[storage.DatasetStore] = None,
        policy: Optional[contract.ValidationPolicy] = None,
        limits: Optional[UploadLimits] = None,
        deployment_check: Optional[DeploymentCheck] = None,
    ) -> None:
        self.session = session
        self.store = store or storage.DatasetStore()
        self.policy = policy or contract.ValidationPolicy.from_env()
        self.limits = limits or UploadLimits.from_env()
        self.deployment_check = deployment_check

    # -- reads ---------------------------------------------------------------

    def get(self, dataset_id: Any) -> TrainingDataset:
        try:
            key = dataset_id if isinstance(dataset_id, uuid.UUID) else uuid.UUID(str(dataset_id))
        except (TypeError, ValueError):
            raise DatasetNotFoundError("No training dataset with that id.", field="dataset_id")
        dataset = self.session.get(TrainingDataset, key)
        if dataset is None:
            raise DatasetNotFoundError("No training dataset with that id.", field="dataset_id")
        return dataset

    def list(self, *, status: Optional[str] = None, limit: int = 100) -> List[TrainingDataset]:
        statement = select(TrainingDataset).order_by(TrainingDataset.created_at.desc()).limit(limit)
        if status:
            statement = statement.where(TrainingDataset.status == status)
        return list(self.session.scalars(statement))

    def processed_frame(self, dataset: TrainingDataset, *, require_ready: bool = True) -> pd.DataFrame:
        """The normalized records, verified against their recorded hash."""
        if require_ready and dataset.status != DatasetStatus.READY.value:
            raise DatasetNotReadyError("This dataset version is not sealed yet.", field="dataset_id")
        if not dataset.processed_object_key:
            raise DatasetNotReadyError("This dataset has no validated records.", field="dataset_id")
        content = self._get(dataset.processed_object_key)
        if contract.sha256_bytes(content) != dataset.processed_object_sha256:
            raise DatasetInvalidError("The stored dataset no longer matches its recorded hash.")
        return contract.from_parquet_bytes(content)

    def history(self, dataset: TrainingDataset, store_id: str, days: int) -> List[Dict[str, Any]]:
        """The last ``days`` recorded days of one store, as forecast history."""
        frame = self.processed_frame(dataset)
        rows = frame[frame["store_id"] == store_id].sort_values("date").tail(days)
        if rows.empty:
            raise ScenarioValidationError(f"Store {store_id} is not in this dataset.", field="store_id")
        return [
            {"date": day.date().isoformat(), "sales": float(sales)}
            for day, sales in zip(rows["date"], rows["sales"])
        ]

    # -- upload --------------------------------------------------------------

    def create_session(
        self,
        *,
        user: User,
        staged: Sequence[StagedFile],
        name: str,
        mode: str,
        source_type: str,
        config: Optional[Dict[str, Any]] = None,
        base_dataset_id: Optional[str] = None,
        conflict_policy: Optional[str] = None,
        upload_key: Optional[str] = None,
    ) -> tuple:
        """Store the originals write-once and record a session. ``(dataset, created)``."""
        if upload_key:
            if not UPLOAD_KEY_PATTERN.match(upload_key):
                raise ScenarioValidationError("Idempotency-Key must be 8-64 letters, digits, '-' or '_'.", field="idempotency_key")
            existing = self.session.scalar(select(TrainingDataset).where(TrainingDataset.upload_key == upload_key))
            if existing is not None:
                if existing.created_by_id != user.id:
                    raise IdempotencyKeyConflictError("This upload key belongs to another request.", field="idempotency_key")
                return existing, False
        name = (name or "").strip()
        if not NAME_PATTERN.match(name):
            raise ScenarioValidationError(
                "Dataset name: letters, digits, spaces, '.', '_' or '-', up to 80 characters.", field="name"
            )
        if mode not in (DatasetMode.INDEPENDENT.value, DatasetMode.APPEND.value):
            raise ScenarioValidationError("Mode must be 'independent' or 'append'.", field="mode")
        if source_type not in contract.SOURCE_TYPES:
            raise ScenarioValidationError("Source type must be 'real' or 'synthetic'.", field="source_type")
        if not staged:
            raise ScenarioValidationError("Choose at least one file.", field="files")
        config = clean_config(config)

        base: Optional[TrainingDataset] = None
        if mode == DatasetMode.APPEND.value:
            if not base_dataset_id:
                raise ScenarioValidationError("Append mode needs the ready dataset to append to.", field="base_dataset_id")
            base = self.get(base_dataset_id)
            if base.status != DatasetStatus.READY.value:
                raise DatasetNotReadyError("Only a sealed (ready) dataset version can be appended to.", field="base_dataset_id")
            conflict_policy = conflict_policy or "reject"
            if conflict_policy not in contract.CONFLICT_POLICIES:
                raise ScenarioValidationError("Conflict policy must be 'reject' or 'prefer_new'.", field="conflict_policy")
        else:
            conflict_policy = None

        unsupported = [_safe_filename(item.original_name) for item in staged if _extension(item.original_name) is None]
        if unsupported:
            # Refused before anything is stored: model, pickle, executable and
            # other non-data files are never accepted.
            raise ScenarioValidationError(
                f"Unsupported file type: {', '.join(name or '?' for name in unsupported)}. "
                "Upload CSV, Parquet, or a ZIP of them.",
                field="files",
                details={"code": "UNSUPPORTED_FILE_TYPE"},
            )
        dataset_id = uuid.uuid4()
        sources: List[Dict[str, Any]] = []
        for index, item in enumerate(staged):
            ext = _extension(item.original_name)
            display = _safe_filename(item.original_name) or f"file-{index}"
            entry = {"name": display, "size": item.size, "sha256": item.sha256, "format": ext}
            if ext is not None:
                key = storage.source_key(dataset_id.hex, index, item.sha256, ext)
                self._put_file(key, item.path, item.sha256, CONTENT_TYPES[ext])
                entry["object_key"] = key
            sources.append(entry)
        package_sha = hashlib.sha256("".join(sorted(s["sha256"] for s in sources)).encode()).hexdigest()
        first = next((s for s in sources if s.get("object_key")), None)

        dataset = TrainingDataset(
            id=dataset_id,
            name=name,
            version=self._next_version(name),
            status=DatasetStatus.PROCESSING.value,
            mode=mode,
            base_dataset_id=base.id if base else None,
            conflict_policy=conflict_policy,
            schema_version=contract.SCHEMA_VERSION,
            target=contract.TARGET,
            currency=str(config.get("currency") or "")[:3] or "---",
            frequency=contract.FREQUENCY,
            source_types=[source_type],
            original_filename=", ".join(s["name"] for s in sources)[:255],
            file_format="package" if len(sources) > 1 else (sources[0]["format"] or "unknown"),
            raw_object_key=first["object_key"] if first else "",
            raw_sha256=package_sha,
            raw_size_bytes=sum(s["size"] for s in sources),
            source_files=sources,
            ingestion_config=config,
            mapping_version=1,
            upload_key=upload_key,
            stores=[],
            validation_report={},
            created_by_id=user.id,
            created_by_email=user.email,
        )
        self.session.add(dataset)
        self.session.flush()
        return dataset, True

    def configure(
        self,
        dataset: TrainingDataset,
        config: Dict[str, Any],
        expected_mapping_version: Optional[int],
    ) -> TrainingDataset:
        """Replace the mapping/confirmations; any earlier validation is void."""
        if dataset.status == DatasetStatus.READY.value:
            raise ScenarioValidationError("A sealed version cannot be re-mapped; upload a new version.", field="config")
        if dataset.status == DatasetStatus.PROCESSING.value:
            raise ScenarioValidationError("Processing is still running; wait for it to finish.", field="config")
        if expected_mapping_version is not None and expected_mapping_version != dataset.mapping_version:
            raise IdempotencyKeyConflictError(
                "The dataset was re-configured since you loaded it; reload and try again.", field="mapping_version"
            )
        merged = {**(dataset.ingestion_config or {}), **clean_config(config)}
        dataset.ingestion_config = {key: value for key, value in merged.items() if value not in (None, "")}
        if "currency" in dataset.ingestion_config:
            dataset.currency = dataset.ingestion_config["currency"][:3]
        self._reset(dataset)
        dataset.mapping_version = (dataset.mapping_version or 1) + 1
        self.session.flush()
        return dataset

    def retry(self, dataset: TrainingDataset) -> TrainingDataset:
        if dataset.status != DatasetStatus.FAILED.value:
            raise ScenarioValidationError("Only a failed processing job can be retried.", field="dataset_id")
        self._reset(dataset)
        self.session.flush()
        return dataset

    def _reset(self, dataset: TrainingDataset) -> None:
        dataset.status = DatasetStatus.PROCESSING.value
        dataset.processing_error = None
        dataset.processing_started_at = None
        dataset.processing_finished_at = None
        dataset.validation_report = {}
        dataset.processed_object_key = None
        dataset.processed_object_sha256 = None
        dataset.content_sha256 = None
        dataset.context_object_key = None
        dataset.context_object_sha256 = None
        dataset.acknowledged_warnings = None
        dataset.row_count = None
        dataset.store_count = None
        dataset.stores = []
        dataset.date_min = None
        dataset.date_max = None

    # -- processing ----------------------------------------------------------

    def process(self, dataset: TrainingDataset) -> TrainingDataset:
        """
        Run the ingestion pipeline for one session and record the outcome.

        A storage outage or an unexpected error marks the session ``failed``
        (retryable) -- it is never reported as invalid data.
        """
        started = time.monotonic()
        dataset.status = DatasetStatus.PROCESSING.value
        dataset.processing_started_at = utcnow()
        dataset.processing_error = None
        self.session.commit()
        workdir = tempfile.mkdtemp(prefix="ingest-")
        try:
            staged = []
            for index, source in enumerate(dataset.source_files or []):
                if not source.get("object_key"):
                    # Unsupported extension: no object was stored; the
                    # inspector reports it from the original name.
                    path = os.path.join(workdir, f"unsupported-{index}")
                    open(path, "wb").close()
                    staged.append(StagedFile(source["name"], path, source["size"], source["sha256"]))
                    continue
                path = os.path.join(workdir, f"source-{index}.{source['format']}")
                self.store.download(source["object_key"], path)
                if _file_sha256(path) != source["sha256"]:
                    raise DatasetInvalidError(f"{source['name']} changed in storage after upload.")
                staged.append(StagedFile(source["name"], path, source["size"], source["sha256"]))
            self._evaluate(dataset, staged, workdir)
            if time.monotonic() - started > self.limits.processing_timeout_seconds:
                raise TimeoutError(
                    f"Processing exceeded {self.limits.processing_timeout_seconds}s; the result was discarded."
                )
        except (storage.StorageUnavailableError, StorageUnavailableError) as exc:
            self._fail(dataset, "STORAGE_UNAVAILABLE", "Dataset storage was unavailable; nothing is wrong with the data. Retry processing.", exc)
        except DatasetInvalidError as exc:
            self._fail(dataset, "PROCESSING_FAILED", exc.message, exc)
        except TimeoutError as exc:
            self._fail(dataset, "PROCESSING_FAILED", str(exc), exc)
        except Exception as exc:  # noqa: BLE001 - any other failure is a processing failure
            logger.exception("Processing training dataset %s failed", dataset.id)
            self._fail(dataset, "PROCESSING_FAILED", f"Processing failed unexpectedly ({type(exc).__name__}). Retry; if it persists, check the API log.", exc)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        dataset.processing_finished_at = utcnow()
        self.session.commit()
        return dataset

    def _fail(self, dataset: TrainingDataset, code: str, message: str, exc: BaseException) -> None:
        logger.warning("Training dataset %s: %s (%s)", dataset.id, code, exc)
        self.session.rollback()
        dataset.status = DatasetStatus.FAILED.value
        dataset.processing_error = f"{code}: {message}"
        dataset.validation_report = {
            "status": "failed",
            "errors": [{"code": code, "severity": "error", "stage": "file", "message": message, "action": "Retry processing.", "count": 0, "examples": []}],
            "warnings": [],
        }

    def _evaluate(self, dataset: TrainingDataset, staged: List[StagedFile], workdir: str) -> None:
        config = dict(dataset.ingestion_config or {})
        package = inspect_package(staged, workdir, self.limits)
        base = self.get(dataset.base_dataset_id) if dataset.base_dataset_id else None
        result = pipeline.process(
            package,
            config,
            self.policy,
            max_rows=self.limits.max_rows,
            source_type=(dataset.source_types or ["real"])[0],
            require_history=base is None,
        )
        report = result.report
        frame = result.frame
        normalized = result.normalized
        if normalized is not None:
            dataset.source_namespace = normalized.source_namespace
            dataset.target_type = normalized.target_type or None
            dataset.source_grain = normalized.source_grain or None
            dataset.feature_options = normalized.options.as_dict()
        detection = report.get("detection") or {}
        selected = detection.get("selected")
        dataset.detection = detection
        if selected:
            from data_ingestion.adapter_registry import get_adapter

            dataset.adapter_id = selected
            dataset.adapter_version = get_adapter(selected).version

        if base is not None and frame is not None:
            frame, report = self._append(dataset, base, frame, report)

        if frame is not None and self.deployment_check is not None:
            try:
                compatibility = self.deployment_check(contract.stores_of(frame), str(config.get("currency") or ""))
            except Exception as exc:  # noqa: BLE001 - informational only
                logger.info("Deployment compatibility check skipped: %s", exc)
            else:
                report["deployment_compatibility"] = compatibility
                if not compatibility.get("compatible"):
                    report["warnings"] = list(report.get("warnings") or []) + [
                        contract.warning(
                            "DEPLOYMENT_DOMAIN_MISMATCH",
                            "Trainable, but a model trained on this data cannot replace the current serving model: "
                            + " ".join(compatibility.get("reasons") or []),
                            stage="deployment",
                        ).as_dict()
                    ]
                report.setdefault("stages", {})["deployment"] = {
                    "status": "passed" if compatibility.get("compatible") else "warning",
                    "issues": 0 if compatibility.get("compatible") else 1,
                }

        if frame is not None and report.get("status") == "valid":
            parquet = contract.to_parquet_bytes(frame)
            content_sha = contract.content_hash(frame)
            object_sha = contract.sha256_bytes(parquet)
            key = storage.processed_key(dataset.id.hex, content_sha)
            self._put(key, parquet, object_sha, CONTENT_TYPES["parquet"])
            dataset.processed_object_key = key
            dataset.processed_object_sha256 = object_sha
            dataset.content_sha256 = content_sha
            context = result.context
            if context is not None and not context.empty:
                buffer = io.BytesIO()
                context.reset_index(drop=True).to_parquet(buffer, index=False)
                context_bytes = buffer.getvalue()
                context_sha = contract.sha256_bytes(context_bytes)
                context_key = storage.context_key(dataset.id.hex, context_sha)
                self._put(context_key, context_bytes, context_sha, CONTENT_TYPES["parquet"])
                dataset.context_object_key = context_key
                dataset.context_object_sha256 = context_sha
            dataset.row_count = int(len(frame))
            dataset.store_count = int(frame["store_id"].nunique())
            dataset.stores = contract.stores_of(frame)
            dataset.date_min = frame["date"].min().date()
            dataset.date_max = frame["date"].max().date()
            dataset.status = DatasetStatus.VALIDATED.value
        elif result.status == "needs_mapping" and not report.get("errors"):
            dataset.status = DatasetStatus.NEEDS_MAPPING.value
        else:
            dataset.status = DatasetStatus.INVALID.value
        report["mapping_version"] = dataset.mapping_version
        report["adapter"] = {"id": dataset.adapter_id, "version": dataset.adapter_version}
        dataset.validation_report = _jsonable(report)

    def _append(self, dataset, base, frame, report):
        combined = contract.combine(
            self.processed_frame(base),
            {
                "schema_version": base.schema_version,
                "target": base.target,
                "frequency": base.frequency,
                "currency": (base.ingestion_config or {}).get("currency") or base.currency,
                "source_types": base.source_types,
                "source_namespace": base.source_namespace or "canonical",
            },
            frame,
            {
                "schema_version": contract.SCHEMA_VERSION,
                "target": contract.TARGET,
                "frequency": contract.FREQUENCY,
                "currency": (dataset.ingestion_config or {}).get("currency"),
                "source_types": dataset.source_types,
                "source_namespace": dataset.source_namespace or "canonical",
            },
            conflict_policy=dataset.conflict_policy or "reject",
        )
        if combined.frame is None:
            report = dict(report)
            report["errors"] = list(report.get("errors") or []) + list(combined.report.get("errors") or [])
            report["status"] = "invalid"
            report["combination"] = combined.report
            report.setdefault("stages", {})["append"] = {"status": "failed", "issues": len(combined.report.get("errors") or [])}
            return None, report
        final = contract.validate(
            contract.frame_to_raw(combined.frame),
            self.policy,
            currency=str((dataset.ingestion_config or {}).get("currency") or ""),
            source_type=(dataset.source_types or ["real"])[0],
            options=contract.FeatureOptions(snap="omit", closure="omit", missing_dates="reject"),
        )
        merged = {
            **report,
            **{key: value for key, value in final.report.items() if key not in ("errors", "warnings")},
            "upload": {key: report.get(key) for key in ("rows_received", "rows_valid", "exact_duplicates_removed", "date_min", "date_max", "store_count")},
            "combination": combined.report,
            "errors": list(report.get("errors") or []) + final.report["errors"],
            "warnings": list(report.get("warnings") or []) + final.report["warnings"] + list(combined.report.get("warnings") or []),
        }
        merged["status"] = "valid" if final.frame is not None else "invalid"
        merged.setdefault("stages", {})["append"] = {"status": "passed" if final.frame is not None else "failed", "issues": 0}
        return final.frame, merged

    # -- seal ----------------------------------------------------------------

    def validate_and_seal(
        self,
        dataset: TrainingDataset,
        *,
        expected_mapping_version: Optional[int] = None,
        acknowledged: Optional[Sequence[str]] = None,
    ) -> TrainingDataset:
        """
        Make a validated session an immutable version.

        Idempotent for a ready version. Refuses a stale view (the mapping changed
        after the caller looked) and unacknowledged warnings that require it.
        """
        if dataset.status == DatasetStatus.READY.value:
            return dataset
        if dataset.status != DatasetStatus.VALIDATED.value:
            raise DatasetNotReadyError(
                f"The dataset is {dataset.status.replace('_', ' ')}; only a validated dataset can become a version.",
                field="dataset_id",
            )
        if expected_mapping_version is not None and expected_mapping_version != dataset.mapping_version:
            raise IdempotencyKeyConflictError(
                "The mapping changed after this validation was shown; review the new result first.",
                field="mapping_version",
            )
        report = dataset.validation_report or {}
        needed = sorted({w["code"] for w in report.get("warnings") or [] if w.get("requires_ack")})
        missing_ack = [code for code in needed if code not in set(acknowledged or [])]
        if missing_ack:
            raise ScenarioValidationError(
                "Acknowledge these warnings before creating the version: " + ", ".join(missing_ack) + ".",
                field="acknowledged_warnings",
                details={"required": needed},
            )
        content = self._get(dataset.processed_object_key)
        if contract.sha256_bytes(content) != dataset.processed_object_sha256:
            raise DatasetInvalidError("The validated dataset changed in storage; reprocess it.")
        dataset.acknowledged_warnings = needed
        dataset.sealed_at = utcnow()
        base = self.get(dataset.base_dataset_id) if dataset.base_dataset_id else None
        manifest = self.manifest(dataset, base)
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode("utf-8")
        manifest_sha = contract.sha256_bytes(manifest_bytes)
        manifest_key = storage.manifest_key(dataset.id.hex)
        self._put(manifest_key, manifest_bytes, manifest_sha, "application/json")
        dataset.manifest_object_key = manifest_key
        dataset.manifest_sha256 = manifest_sha
        dataset.status = DatasetStatus.READY.value
        self.session.flush()
        return dataset

    def manifest(self, dataset: TrainingDataset, base: Optional[TrainingDataset]) -> Dict[str, Any]:
        report = dataset.validation_report or {}
        sources: List[Dict[str, Any]] = [
            {"kind": "upload", "name": item.get("name"), "format": item.get("format"), "object_key": item.get("object_key"), "sha256": item.get("sha256"), "size_bytes": item.get("size")}
            for item in dataset.source_files or []
        ]
        if base is not None:
            sources.insert(
                0,
                {
                    "kind": "dataset",
                    "dataset_id": base.id.hex,
                    "name": base.name,
                    "version": base.version,
                    "content_sha256": base.content_sha256,
                    "manifest_sha256": base.manifest_sha256,
                },
            )
        return {
            "manifest_version": MANIFEST_VERSION,
            "dataset_id": dataset.id.hex,
            "name": dataset.name,
            "version": dataset.version,
            "mode": dataset.mode,
            "conflict_policy": dataset.conflict_policy,
            "schema_version": dataset.schema_version,
            "target": dataset.target,
            "target_type": dataset.target_type or contract.TARGET_TYPE,
            "target_description": contract.TARGET_DESCRIPTION,
            "currency": (dataset.ingestion_config or {}).get("currency") or dataset.currency,
            "frequency": dataset.frequency,
            "source_namespace": dataset.source_namespace,
            "source_grain": dataset.source_grain,
            "source_types": list(dataset.source_types or []),
            "adapter": {"id": dataset.adapter_id, "version": dataset.adapter_version},
            "mapping": {"version": dataset.mapping_version, "config": dataset.ingestion_config, "applied": report.get("mapping")},
            "transformations": report.get("transformations"),
            "retained_columns": report.get("retained_columns"),
            "feature_options": dataset.feature_options,
            "sources": sources,
            "processed": {
                "object_key": dataset.processed_object_key,
                "object_sha256": dataset.processed_object_sha256,
                "content_sha256": dataset.content_sha256,
                "format": "parquet",
            },
            "context": (
                {"object_key": dataset.context_object_key, "object_sha256": dataset.context_object_sha256, "used_by_model": False}
                if dataset.context_object_key
                else None
            ),
            "date_min": dataset.date_min.isoformat() if dataset.date_min else None,
            "date_max": dataset.date_max.isoformat() if dataset.date_max else None,
            "row_count": dataset.row_count,
            "store_count": dataset.store_count,
            "stores": list(dataset.stores or []),
            "stores_excluded": report.get("stores_excluded") or [],
            "context_features": report.get("context_features") or {},
            "validation": {
                "status": report.get("status"),
                "warnings": [issue.get("code") for issue in report.get("warnings") or []],
                "acknowledged": dataset.acknowledged_warnings or [],
                "split": report.get("split"),
                "policy": report.get("policy"),
                "combination": report.get("combination"),
            },
            "created_at": dataset.created_at.isoformat() if dataset.created_at else None,
            "created_by": {"id": str(dataset.created_by_id) if dataset.created_by_id else None, "email": dataset.created_by_email},
            "sealed_at": dataset.sealed_at.isoformat() if dataset.sealed_at else None,
        }

    # -- internals -----------------------------------------------------------

    def _next_version(self, name: str) -> int:
        current = self.session.scalar(select(func.max(TrainingDataset.version)).where(TrainingDataset.name == name))
        return int(current or 0) + 1

    def _put(self, key: str, content: bytes, sha: str, content_type: str) -> None:
        try:
            self.store.put_once(key, content, sha, content_type)
        except storage.ImmutableObjectError as exc:
            raise DatasetInvalidError("A stored dataset object would have been overwritten; refused.") from exc
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError("Dataset storage is unavailable.") from exc

    def _put_file(self, key: str, path: str, sha: str, content_type: str) -> None:
        try:
            if hasattr(self.store, "put_file_once"):
                self.store.put_file_once(key, path, sha, content_type)
            else:
                with open(path, "rb") as handle:
                    self.store.put_once(key, handle.read(), sha, content_type)
        except storage.ImmutableObjectError as exc:
            raise DatasetInvalidError("A stored dataset object would have been overwritten; refused.") from exc
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError("Dataset storage is unavailable.") from exc

    def _get(self, key: str) -> bytes:
        try:
            return self.store.get(key)
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError("Dataset storage is unavailable.") from exc


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    """Make a report safe for a JSON column (dates, numpy scalars, NaN)."""
    return _drop_nan(json.loads(json.dumps(value, default=_default)))


def _drop_nan(value: Any) -> Any:
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, dict):
        return {key: _drop_nan(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_drop_nan(item) for item in value]
    return value


def _default(value: Any) -> Any:
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    if value is pd.NA:
        return None
    return str(value)
