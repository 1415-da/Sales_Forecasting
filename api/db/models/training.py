"""
Training datasets, training requests, and deployment decisions.

Three records, one per question an auditor asks:

* ``training_datasets``     -- what data, exactly (hashes, source, validation)
* ``training_requests``     -- who trained what, how, and what came out
* ``deployment_decisions``  -- who approved or rejected a candidate, and why

A dataset row is immutable once ``ready``: its object keys and hashes are the
dataset, and a correction is a new version with a new row. The request row is
the one that changes, as the Airflow run progresses.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import JSON_TYPE, TIMESTAMP_TYPE, Base, utcnow
from api.db.models.user import USERS_TABLE

TRAINING_DATASETS_TABLE = "training_datasets"
TRAINING_REQUESTS_TABLE = "training_requests"
DEPLOYMENT_DECISIONS_TABLE = "deployment_decisions"


class DatasetStatus(str, Enum):
    """``validated`` means checked but not yet sealed; ``ready`` is immutable."""

    INVALID = "invalid"
    VALIDATED = "validated"
    READY = "ready"


class DatasetMode(str, Enum):
    INDEPENDENT = "independent"
    APPEND = "append"


class TrainingStatus(str, Enum):
    """
    A training request's lifecycle.

    ``submission_uncertain`` is the honest state after a timeout: the request
    may or may not have reached Airflow, and only a lookup by the known run id
    can say which.
    """

    PENDING = "pending"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (TrainingStatus.SUCCEEDED, TrainingStatus.FAILED)


class DeploymentState(str, Enum):
    NOT_READY = "not_ready"
    AWAITING_REVIEW = "awaiting_review"
    DEPLOYED = "deployed"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class DecisionKind(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


def _user_fk() -> Any:
    return ForeignKey(f"{USERS_TABLE}.id", ondelete="SET NULL")


class TrainingDataset(Base):
    """One immutable version of a training dataset."""

    __tablename__ = TRAINING_DATASETS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    base_dataset_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey(f"{TRAINING_DATASETS_TABLE}.id", ondelete="RESTRICT"), nullable=True
    )
    conflict_policy: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    frequency: Mapped[str] = mapped_column(String(8), nullable=False)
    source_types: Mapped[List[str]] = mapped_column(JSON_TYPE, nullable=False, default=list)

    #: Display only. Never used to build a storage key or a path.
    original_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    file_format: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_object_key: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    processed_object_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    processed_object_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    content_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    manifest_object_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    manifest_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    date_min: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    date_max: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    store_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stores: Mapped[List[str]] = mapped_column(JSON_TYPE, nullable=False, default=list)

    validation_report: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP_TYPE, nullable=False, default=utcnow)
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, _user_fk(), nullable=True)
    created_by_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    sealed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP_TYPE, nullable=True)

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_training_dataset_name_version"),
    )


class TrainingRequest(Base):
    """One request to train on a ready dataset, and everything it produced."""

    __tablename__ = TRAINING_REQUESTS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    #: Client-supplied key; a repeated submit with the same key returns the
    #: same request instead of starting a second run.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey(f"{TRAINING_DATASETS_TABLE}.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dataset_name: Mapped[str] = mapped_column(String(80), nullable=False)
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    training_config_id: Mapped[str] = mapped_column(String(32), nullable=False)
    training_config: Mapped[Dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    deployment_policy: Mapped[str] = mapped_column(String(32), nullable=False)

    dag_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dag_run_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    airflow_state: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    task_states: Mapped[List[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP_TYPE, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP_TYPE, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP_TYPE, nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP_TYPE, nullable=True)

    mlflow_run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    #: ``[{model, registered_name, version, gate_passed}]``
    candidates: Mapped[List[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    #: Test-window metrics, quality gate, and champion comparison.
    evaluation: Mapped[Dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)

    deployment_state: Mapped[str] = mapped_column(String(32), nullable=False)
    deployed_model: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    deployed_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP_TYPE, nullable=False, default=utcnow, index=True)
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, _user_fk(), nullable=True)
    created_by_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    created_by_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("ix_training_requests_status_created", "status", "created_at"),
    )


class DeploymentDecision(Base):
    """An approval or rejection of one candidate model version."""

    __tablename__ = DEPLOYMENT_DECISIONS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    training_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey(f"{TRAINING_REQUESTS_TABLE}.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    previous_production_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: Smoke test and compatibility gate results, as evaluated at decision time.
    checks: Mapped[Dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)

    decided_at: Mapped[datetime] = mapped_column(TIMESTAMP_TYPE, nullable=False, default=utcnow)
    decided_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, _user_fk(), nullable=True)
    decided_by_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    decided_by_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
