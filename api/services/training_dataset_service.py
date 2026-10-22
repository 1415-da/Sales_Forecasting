"""
Training dataset versions: upload, validate, combine, seal.

Two steps, deliberately separate:

1. **Upload** stores the raw bytes under a generated key and validates them.
   The result is a ``validated`` (or ``invalid``) draft with its report. Nothing
   trainable exists yet.
2. **Validate** re-reads the stored bytes, validates again (and, in append
   mode, combines with the base version), then *seals*: the processed Parquet
   and the manifest are written once, their hashes recorded, and the row
   becomes ``ready``. A ready version never changes; new data is a new version.

The second validation is not redundant. It runs against what is in storage,
not what the browser sent, and it is the moment the policy in force is
recorded in the manifest.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.core.errors import (
    DatasetInvalidError,
    DatasetNotFoundError,
    DatasetNotReadyError,
    ScenarioValidationError,
    StorageUnavailableError,
    UploadTooLargeError,
)
from api.db.base import utcnow
from api.db.models.training import DatasetMode, DatasetStatus, TrainingDataset
from api.db.models.user import User
from training import contract, storage

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,79}$")
MANIFEST_VERSION = 1


def _safe_filename(filename: Optional[str]) -> Optional[str]:
    """The upload's name for display: basename only, printable, bounded."""
    if not filename:
        return None
    base = re.split(r"[\\/]", filename)[-1]
    cleaned = "".join(ch for ch in base if ch.isprintable())[:255]
    return cleaned or None


class TrainingDatasetService:
    def __init__(
        self,
        session: Session,
        store: Optional[storage.DatasetStore] = None,
        policy: Optional[contract.ValidationPolicy] = None,
    ) -> None:
        self.session = session
        self.store = store or storage.DatasetStore()
        self.policy = policy or contract.ValidationPolicy.from_env()

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

    def processed_frame(self, dataset: TrainingDataset) -> pd.DataFrame:
        """The sealed records, verified against their recorded hashes."""
        if dataset.status != DatasetStatus.READY.value or not dataset.processed_object_key:
            raise DatasetNotReadyError("This dataset version is not sealed yet.", field="dataset_id")
        content = self._get(dataset.processed_object_key)
        if contract.sha256_bytes(content) != dataset.processed_object_sha256:
            raise DatasetInvalidError("The stored dataset no longer matches its recorded hash.")
        return contract.from_parquet_bytes(content)

    def history(self, dataset: TrainingDataset, store_id: str, days: int) -> List[Dict[str, Any]]:
        """The last ``days`` recorded days of one store, as forecast history."""
        frame = self.processed_frame(dataset)
        rows = frame[frame["store_id"] == store_id].sort_values("date").tail(days)
        if rows.empty:
            raise ScenarioValidationError(
                f"Store {store_id} is not in this dataset.", field="store_id"
            )
        return [
            {"date": day.date().isoformat(), "sales": float(sales)}
            for day, sales in zip(rows["date"], rows["sales"])
        ]

    # -- upload --------------------------------------------------------------

    def upload(
        self,
        *,
        user: User,
        filename: Optional[str],
        content: bytes,
        name: str,
        mode: str,
        currency: str,
        source_type: str,
        base_dataset_id: Optional[str] = None,
        conflict_policy: Optional[str] = None,
    ) -> TrainingDataset:
        name = (name or "").strip()
        if not NAME_PATTERN.match(name):
            raise ScenarioValidationError(
                "Dataset name: letters, digits, spaces, '.', '_' or '-', up to 80 characters.",
                field="name",
            )
        if mode not in (DatasetMode.INDEPENDENT.value, DatasetMode.APPEND.value):
            raise ScenarioValidationError("Mode must be 'independent' or 'append'.", field="mode")
        currency = (currency or "").strip().upper()
        if len(content) > self.policy.max_bytes:
            raise UploadTooLargeError(
                f"The file is {len(content):,} bytes; the limit is {self.policy.max_bytes:,}.",
                field="file",
            )
        if not content:
            raise ScenarioValidationError("The file is empty.", field="file")

        base: Optional[TrainingDataset] = None
        if mode == DatasetMode.APPEND.value:
            if not base_dataset_id:
                raise ScenarioValidationError(
                    "Append mode needs the ready dataset to append to.", field="base_dataset_id"
                )
            base = self.get(base_dataset_id)
            if base.status != DatasetStatus.READY.value:
                raise DatasetNotReadyError(
                    "Only a sealed (ready) dataset version can be appended to.",
                    field="base_dataset_id",
                )
            conflict_policy = conflict_policy or "reject"
            if conflict_policy not in contract.CONFLICT_POLICIES:
                raise ScenarioValidationError(
                    "Conflict policy must be 'reject' or 'prefer_new'.", field="conflict_policy"
                )
        else:
            conflict_policy = None

        try:
            file_format = contract.detect_format(filename or "", content)
        except contract.UploadReadError as exc:
            raise ScenarioValidationError(str(exc), field="file") from exc

        dataset_id = uuid.uuid4()
        raw_sha = contract.sha256_bytes(content)
        raw_key = storage.raw_key(dataset_id.hex, raw_sha, file_format)
        self._put(
            raw_key,
            content,
            raw_sha,
            "text/csv" if file_format == "csv" else "application/vnd.apache.parquet",
        )

        dataset = TrainingDataset(
            id=dataset_id,
            name=name,
            version=self._next_version(name),
            status=DatasetStatus.INVALID.value,
            mode=mode,
            base_dataset_id=base.id if base else None,
            conflict_policy=conflict_policy,
            schema_version=contract.SCHEMA_VERSION,
            target=contract.TARGET,
            currency=currency,
            frequency=contract.FREQUENCY,
            source_types=[source_type] if source_type in contract.SOURCE_TYPES else [],
            original_filename=_safe_filename(filename),
            file_format=file_format,
            raw_object_key=raw_key,
            raw_sha256=raw_sha,
            raw_size_bytes=len(content),
            stores=[],
            validation_report={},
            created_by_id=user.id,
            created_by_email=user.email,
        )
        self._evaluate(dataset, content, base)
        self.session.add(dataset)
        self.session.flush()
        return dataset

    # -- validate and seal ---------------------------------------------------

    def validate_and_seal(self, dataset: TrainingDataset) -> TrainingDataset:
        """
        Re-validate from storage and, if clean, make the version immutable.

        Idempotent: a ready version is returned untouched.
        """
        if dataset.status == DatasetStatus.READY.value:
            return dataset
        content = self._get(dataset.raw_object_key)
        if contract.sha256_bytes(content) != dataset.raw_sha256:
            raise DatasetInvalidError("The stored upload no longer matches its recorded hash.")
        base = self.get(dataset.base_dataset_id) if dataset.base_dataset_id else None
        frame = self._evaluate(dataset, content, base)
        if frame is None:
            self.session.flush()
            return dataset

        parquet = contract.to_parquet_bytes(frame)
        content_sha = contract.content_hash(frame)
        object_sha = contract.sha256_bytes(parquet)
        processed_key = storage.processed_key(dataset.id.hex, content_sha)
        self._put(processed_key, parquet, object_sha, "application/vnd.apache.parquet")

        dataset.processed_object_key = processed_key
        dataset.processed_object_sha256 = object_sha
        dataset.content_sha256 = content_sha
        dataset.sealed_at = utcnow()

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
        sources: List[Dict[str, Any]] = [
            {
                "kind": "upload",
                "original_filename": dataset.original_filename,
                "file_format": dataset.file_format,
                "raw_object_key": dataset.raw_object_key,
                "raw_sha256": dataset.raw_sha256,
                "size_bytes": dataset.raw_size_bytes,
            }
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
        report = dataset.validation_report or {}
        return {
            "manifest_version": MANIFEST_VERSION,
            "dataset_id": dataset.id.hex,
            "name": dataset.name,
            "version": dataset.version,
            "mode": dataset.mode,
            "conflict_policy": dataset.conflict_policy,
            "schema_version": dataset.schema_version,
            "target": dataset.target,
            "target_description": contract.TARGET_DESCRIPTION,
            "currency": dataset.currency,
            "frequency": dataset.frequency,
            "source_types": list(dataset.source_types or []),
            "sources": sources,
            "processed": {
                "object_key": dataset.processed_object_key,
                "object_sha256": dataset.processed_object_sha256,
                "content_sha256": dataset.content_sha256,
                "format": "parquet",
            },
            "date_min": dataset.date_min.isoformat() if dataset.date_min else None,
            "date_max": dataset.date_max.isoformat() if dataset.date_max else None,
            "row_count": dataset.row_count,
            "store_count": dataset.store_count,
            "stores": list(dataset.stores or []),
            "context_features": report.get("context_features") or {},
            "validation": {
                "status": report.get("status"),
                "warnings": [issue.get("code") for issue in report.get("warnings") or []],
                "split": report.get("split"),
                "policy": report.get("policy"),
                "combination": report.get("combination"),
            },
            "created_at": dataset.created_at.isoformat() if dataset.created_at else None,
            "created_by": {
                "id": str(dataset.created_by_id) if dataset.created_by_id else None,
                "email": dataset.created_by_email,
            },
            "sealed_at": dataset.sealed_at.isoformat() if dataset.sealed_at else None,
        }

    # -- internals -----------------------------------------------------------

    def _evaluate(
        self, dataset: TrainingDataset, content: bytes, base: Optional[TrainingDataset]
    ) -> Optional[pd.DataFrame]:
        """Validate (and combine) the upload; record the report. Clean frame or None."""
        source_type = (dataset.source_types or [""])[0] if dataset.source_types else ""
        try:
            raw = contract.read_table(content, dataset.file_format)
        except contract.UploadReadError as exc:
            dataset.status = DatasetStatus.INVALID.value
            dataset.validation_report = {
                "status": "invalid",
                "schema_version": contract.SCHEMA_VERSION,
                "errors": [{"code": "unreadable_file", "message": str(exc), "count": 0, "examples": []}],
                "warnings": [],
                "policy": self.policy.describe(),
            }
            return None
        result = contract.validate(
            raw,
            self.policy,
            currency=dataset.currency,
            source_type=source_type,
            require_history=base is None,
        )
        report = dict(result.report)
        frame = result.frame

        if base is not None and frame is not None:
            upload_report = report
            combined = contract.combine(
                self.processed_frame(base),
                {
                    "schema_version": base.schema_version,
                    "target": base.target,
                    "frequency": base.frequency,
                    "currency": base.currency,
                    "source_types": base.source_types,
                },
                frame,
                {
                    "schema_version": contract.SCHEMA_VERSION,
                    "target": contract.TARGET,
                    "frequency": contract.FREQUENCY,
                    "currency": dataset.currency,
                    "source_types": dataset.source_types,
                },
                conflict_policy=dataset.conflict_policy or "reject",
            )
            if combined.frame is None:
                report = {
                    **upload_report,
                    "status": "invalid",
                    "errors": list(upload_report.get("errors") or []) + list(combined.report.get("errors") or []),
                    "combination": combined.report,
                }
                frame = None
            else:
                final = contract.validate(
                    contract.frame_to_raw(combined.frame),
                    self.policy,
                    currency=dataset.currency,
                    source_type=source_type,
                )
                report = {
                    **final.report,
                    "rows_received": upload_report.get("rows_received"),
                    "upload": {
                        key: upload_report.get(key)
                        for key in ("rows_received", "rows_valid", "exact_duplicates_removed", "date_min", "date_max", "store_count", "warnings")
                    },
                    "combination": combined.report,
                }
                frame = final.frame

        dataset.validation_report = _jsonable(report)
        if frame is None:
            dataset.status = DatasetStatus.INVALID.value
            return None
        dataset.status = DatasetStatus.VALIDATED.value
        dataset.row_count = int(len(frame))
        dataset.store_count = int(frame["store_id"].nunique())
        dataset.stores = contract.stores_of(frame)
        dataset.date_min = frame["date"].min().date()
        dataset.date_max = frame["date"].max().date()
        return frame

    def _next_version(self, name: str) -> int:
        current = self.session.scalar(
            select(func.max(TrainingDataset.version)).where(TrainingDataset.name == name)
        )
        return int(current or 0) + 1

    def _put(self, key: str, content: bytes, sha: str, content_type: str) -> None:
        try:
            self.store.put_once(key, content, sha, content_type)
        except storage.ImmutableObjectError as exc:
            raise DatasetInvalidError(
                "A stored dataset object would have been overwritten; refused."
            ) from exc
        except (storage.StorageUnavailableError, Exception) as exc:  # noqa: BLE001
            if isinstance(exc, DatasetInvalidError):
                raise
            raise StorageUnavailableError("Dataset storage is unavailable.") from exc

    def _get(self, key: str) -> bytes:
        try:
            return self.store.get(key)
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError("Dataset storage is unavailable.") from exc


def _jsonable(value: Any) -> Any:
    """Make a report safe for a JSON column (dates, numpy scalars)."""
    return json.loads(json.dumps(value, default=_default))


def _default(value: Any) -> Any:
    if isinstance(value, (date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)
