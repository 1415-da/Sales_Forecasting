"""Request and response bodies for the training workspace."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class TrainingConfigOut(BaseModel):
    id: str
    label: str
    description: str
    use_optuna: bool
    optuna_trials: int
    models: List[str]
    evaluation_policy: str
    deployment_policy: str
    expected_duration: str
    min_test_rows: int
    allowed: bool = Field(description="Whether the signed-in user may request this configuration.")


class TrainingContractResponse(BaseModel):
    contract: Dict[str, Any]
    configs: List[TrainingConfigOut]
    source_types: List[str]
    conflict_policies: List[str]
    workflows: Dict[str, str]
    limits: Dict[str, Any] = Field(default_factory=dict)
    adapters: List[Dict[str, str]] = Field(default_factory=list)
    serving_currencies: List[str] = Field(default_factory=list)


class DatasetOut(BaseModel):
    id: str
    name: str
    version: int
    status: str
    mode: str
    base_dataset_id: Optional[str] = None
    conflict_policy: Optional[str] = None
    schema_version: str
    target: str
    target_type: Optional[str] = None
    #: The confirmed currency, ``UNKNOWN`` when explicitly unknown, or null
    #: while not yet confirmed.
    currency: Optional[str] = None
    frequency: str
    source_types: List[str]
    source_namespace: Optional[str] = None
    source_grain: Optional[str] = None
    adapter_id: Optional[str] = None
    adapter_version: Optional[str] = None
    mapping_version: Optional[int] = None
    source_files: List[Dict[str, Any]] = Field(default_factory=list)
    processing_error: Optional[str] = None
    processing_started_at: Optional[datetime] = None
    processing_finished_at: Optional[datetime] = None
    original_filename: Optional[str] = None
    file_format: str
    raw_sha256: str
    raw_size_bytes: int
    content_sha256: Optional[str] = None
    manifest_sha256: Optional[str] = None
    manifest_uri: Optional[str] = None
    date_min: Optional[date] = None
    date_max: Optional[date] = None
    row_count: Optional[int] = None
    store_count: Optional[int] = None
    stores: List[str] = Field(default_factory=list)
    created_at: datetime
    created_by: Optional[str] = None
    sealed_at: Optional[datetime] = None
    error_count: int = 0
    warning_count: int = 0


class DatasetDetail(DatasetOut):
    validation_report: Dict[str, Any] = Field(default_factory=dict)
    ingestion_config: Dict[str, Any] = Field(default_factory=dict)
    feature_options: Optional[Dict[str, Any]] = None
    acknowledged_warnings: List[str] = Field(default_factory=list)


class DatasetConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Adapter choice, column mapping, confirmations and policies.
    config: Dict[str, Any] = Field(default_factory=dict)
    #: The mapping version the caller is looking at; a mismatch is refused.
    expected_mapping_version: Optional[int] = None


class DatasetSealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_mapping_version: Optional[int] = None
    acknowledged_warnings: List[str] = Field(default_factory=list)


class DatasetListResponse(BaseModel):
    datasets: List[DatasetOut]


class HistoryPoint(BaseModel):
    date: date
    sales: float


class DatasetHistoryResponse(BaseModel):
    dataset_id: str
    dataset_label: str
    store_id: str
    history: List[HistoryPoint]


class TrainingRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=32, max_length=36)
    training_config_id: str = Field(min_length=1, max_length=32)


class DecisionOut(BaseModel):
    decision: str
    model: str
    version: str
    reason: str
    previous_production_version: Optional[str] = None
    checks: Dict[str, Any] = Field(default_factory=dict)
    decided_at: datetime
    decided_by: Optional[str] = None
    decided_by_role: Optional[str] = None


class ProductionModelOut(BaseModel):
    model: str
    version: str
    stage: str
    run_id: Optional[str] = None
    metrics: Dict[str, float] = Field(default_factory=dict)
    dataset_version: Optional[str] = None


class TrainingRunOut(BaseModel):
    id: str
    dataset_id: str
    dataset_name: str
    dataset_version: int
    dataset_content_sha256: str
    training_config_id: str
    training_config: Dict[str, Any]
    deployment_policy: str
    dag_id: str
    dag_run_id: str
    status: str
    airflow_state: Optional[str] = None
    failure_reason: Optional[str] = None
    created_at: datetime
    created_by: Optional[str] = None
    created_by_role: Optional[str] = None
    submitted_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None
    mlflow_run_id: Optional[str] = None
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    deployment_state: str
    deployed_model: Optional[str] = None
    deployed_version: Optional[str] = None
    headline_wape: Optional[float] = None
    is_terminal: bool


class TrainingRunDetail(TrainingRunOut):
    task_states: List[Dict[str, Any]] = Field(default_factory=list)
    evaluation: Dict[str, Any] = Field(default_factory=dict)
    decisions: List[DecisionOut] = Field(default_factory=list)
    production: List[ProductionModelOut] = Field(default_factory=list)
    airflow_url: Optional[str] = None
    mlflow_url: Optional[str] = None
    sync_warning: Optional[str] = None
    can_deploy: bool = False


class TrainingRunListResponse(BaseModel):
    runs: List[TrainingRunOut]


class RejectCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str = Field(min_length=1, max_length=32)
    version: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=5, max_length=1000)
