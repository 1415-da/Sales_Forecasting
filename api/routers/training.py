"""
Training workspace: dataset upload sessions, versions, and training requests.

Three workflows in this platform accept sales data, and they are kept apart by
route, permission, and record:

* ``/v1/training/datasets``  -- historical data to *train* a model on
  (this router; persisted as upload sessions and immutable versions);
* ``POST /v1/forecast``      -- history for *one* forecast, never stored;
* monitoring actuals         -- observed outcomes scored against forecasts
  (the ``actuals`` table, loaded by the monitoring DAG).

An upload is a session: files are streamed to staging and stored write-once,
then processed (inline when small, otherwise by a bounded background worker)
through package inspection, adapter detection, normalization and the strict
store_day_v1 validator. The browser polls the session. Deployment is not here:
candidates are deployed through ``POST /v1/models/{version}/promote``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from api.core.errors import ScenarioValidationError, UploadTooLargeError
from api.core.rbac import Permission, has_permission
from api.db.models.audit import AuditAction, ResourceType
from api.db.models.training import DatasetStatus, DecisionKind, TrainingDataset, TrainingRequest, TrainingStatus
from api.db.models.user import User
from api.db.session import get_session
from api.dependencies.auth import actor_for, require_permission
from api.deps import get_airflow_client, get_dataset_store, get_forecast_service
from api.routers.models import mlflow_browse_url
from api.schemas.training import (
    DatasetConfigureRequest,
    DatasetDetail,
    DatasetHistoryResponse,
    DatasetListResponse,
    DatasetOut,
    DatasetSealRequest,
    DecisionOut,
    ProductionModelOut,
    RejectCandidateRequest,
    TrainingConfigOut,
    TrainingContractResponse,
    TrainingRunCreate,
    TrainingRunDetail,
    TrainingRunListResponse,
    TrainingRunOut,
)
from api.services import ingestion_jobs
from api.services.audit_service import AuditService
from api.services.deployment_gate_service import dataset_deployment_compatibility
from api.services.forecast_service import ForecastService
from api.services.training_dataset_service import TrainingDatasetService
from api.services.training_service import TrainingService
from data_ingestion.adapter_registry import ADAPTERS
from data_ingestion.limits import UploadLimits
from data_ingestion.package_inspector import StagedFile
from training import contract, storage
from training.configs import TRAINING_CONFIGS

logger = logging.getLogger(__name__)

TAG = "Training"
router = APIRouter(prefix="/v1/training", tags=[TAG])

#: Non-terminal runs refreshed from Airflow on a list read.
LIST_SYNC_LIMIT = 10
CHUNK = 1024 * 1024

WORKFLOWS = {
    "training_upload": (
        "Historical store-day sales to train a new model on. Validated, versioned and "
        "stored permanently; used only by training runs."
    ),
    "forecast_history": (
        "The recent history pasted or uploaded in the Forecast Console for one forecast. "
        "Not stored and never used for training."
    ),
    "monitoring_actuals": (
        "Observed sales loaded by the monitoring DAG and scored against past forecasts. "
        "Not used for training."
    ),
}


def _service_factory(store, forecasts: Optional[ForecastService]):
    """Builds a dataset service bound to a session (for request or worker)."""

    def build(session: Session) -> TrainingDatasetService:
        service = TrainingDatasetService(session, store=store)
        if forecasts is not None:
            allowed = list(service.policy.allowed_currencies)
            service.deployment_check = lambda stores, currency: dataset_deployment_compatibility(
                forecasts, allowed, stores, currency
            )
        return service

    return build


def _datasets(session: Session, store, forecasts: Optional[ForecastService] = None) -> TrainingDatasetService:
    return _service_factory(store, forecasts)(session)


def _dataset_out(dataset: TrainingDataset, detail: bool = False):
    report = dataset.validation_report or {}
    config = dataset.ingestion_config or {}
    data = dict(
        id=dataset.id.hex,
        name=dataset.name,
        version=dataset.version,
        status=dataset.status,
        mode=dataset.mode,
        base_dataset_id=dataset.base_dataset_id.hex if dataset.base_dataset_id else None,
        conflict_policy=dataset.conflict_policy,
        schema_version=dataset.schema_version,
        target=dataset.target,
        target_type=dataset.target_type,
        currency=config.get("currency") or (dataset.currency if dataset.currency not in ("---",) else None),
        frequency=dataset.frequency,
        source_types=list(dataset.source_types or []),
        source_namespace=dataset.source_namespace,
        source_grain=dataset.source_grain,
        adapter_id=dataset.adapter_id,
        adapter_version=dataset.adapter_version,
        mapping_version=dataset.mapping_version,
        original_filename=dataset.original_filename,
        file_format=dataset.file_format,
        source_files=[
            {key: item.get(key) for key in ("name", "size", "sha256", "format")}
            for item in dataset.source_files or []
        ],
        raw_sha256=dataset.raw_sha256,
        raw_size_bytes=dataset.raw_size_bytes,
        content_sha256=dataset.content_sha256,
        manifest_sha256=dataset.manifest_sha256,
        manifest_uri=storage.to_uri(dataset.manifest_object_key) if dataset.manifest_object_key else None,
        date_min=dataset.date_min,
        date_max=dataset.date_max,
        row_count=dataset.row_count,
        store_count=dataset.store_count,
        stores=list(dataset.stores or [])[:200],
        created_at=dataset.created_at,
        created_by=dataset.created_by_email,
        sealed_at=dataset.sealed_at,
        processing_error=dataset.processing_error,
        processing_started_at=dataset.processing_started_at,
        processing_finished_at=dataset.processing_finished_at,
        error_count=len(report.get("errors") or []),
        warning_count=len(report.get("warnings") or []),
    )
    if detail:
        return DatasetDetail(
            **data,
            validation_report=report,
            ingestion_config=config,
            feature_options=dataset.feature_options,
            acknowledged_warnings=dataset.acknowledged_warnings or [],
        )
    return DatasetOut(**data)


def _headline_wape(row: TrainingRequest) -> Optional[float]:
    metrics = ((row.evaluation or {}).get("metrics") or {}).get("ensemble") or {}
    value = metrics.get("wape")
    return float(value) if isinstance(value, (int, float)) else None


def _run_fields(row: TrainingRequest) -> dict:
    return dict(
        id=str(row.id),
        dataset_id=row.dataset_id.hex,
        dataset_name=row.dataset_name,
        dataset_version=row.dataset_version,
        dataset_content_sha256=row.dataset_content_sha256,
        training_config_id=row.training_config_id,
        training_config=row.training_config or {},
        deployment_policy=row.deployment_policy,
        dag_id=row.dag_id,
        dag_run_id=row.dag_run_id,
        status=row.status,
        airflow_state=row.airflow_state,
        failure_reason=row.failure_reason,
        created_at=row.created_at,
        created_by=row.created_by_email,
        created_by_role=row.created_by_role,
        submitted_at=row.submitted_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        last_synced_at=row.last_synced_at,
        mlflow_run_id=row.mlflow_run_id,
        candidates=list(row.candidates or []),
        deployment_state=row.deployment_state,
        deployed_model=row.deployed_model,
        deployed_version=row.deployed_version,
        headline_wape=_headline_wape(row),
        is_terminal=TrainingStatus(row.status).is_terminal,
    )


def _production(forecasts: ForecastService) -> List[ProductionModelOut]:
    out: List[ProductionModelOut] = []
    for model in forecasts.registry.known_models():
        try:
            versions = forecasts.registry.list_versions(model)
        except Exception as exc:  # noqa: BLE001 - shown as absent, not as an error
            logger.info("Could not list %s versions: %s", model, exc)
            continue
        for item in versions:
            if item.is_production:
                out.append(
                    ProductionModelOut(
                        model=model,
                        version=item.version,
                        stage=item.stage,
                        run_id=item.run_id,
                        metrics={k: float(v) for k, v in (item.metrics or {}).items() if isinstance(v, (int, float))},
                        dataset_version=item.lineage().get("dataset_version"),
                    )
                )
                break
    return out


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@router.get("/contract", response_model=TrainingContractResponse, summary="Accepted training data, limits, adapters and configurations")
def get_contract(
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
) -> TrainingContractResponse:
    """The canonical schema, adapters, upload limits, validation policy and approved configurations."""
    policy = contract.ValidationPolicy.from_env()
    configs = [
        TrainingConfigOut(
            **{key: value for key, value in config.as_dict().items() if key in TrainingConfigOut.model_fields},
            allowed=has_permission(user.role_enum, Permission(config.required_permission)),
        )
        for config in TRAINING_CONFIGS.values()
    ]
    return TrainingContractResponse(
        contract=contract.contract_description(policy),
        configs=configs,
        source_types=list(contract.SOURCE_TYPES),
        conflict_policies=list(contract.CONFLICT_POLICIES),
        workflows=WORKFLOWS,
        limits=UploadLimits.from_env().as_dict(),
        adapters=[{"id": item.id, "version": item.version, "label": item.label} for item in ADAPTERS.values()],
        serving_currencies=list(policy.allowed_currencies),
    )


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


async def _stage(uploads: List[UploadFile], workdir: str, limits: UploadLimits) -> List[StagedFile]:
    """Stream each upload to disk, hashing as it goes; enforce byte limits."""
    staged: List[StagedFile] = []
    total = 0
    if len(uploads) > limits.max_files:
        raise UploadTooLargeError(
            f"{len(uploads)} files were sent; the limit is {limits.max_files}. Put related files in one ZIP.",
            field="files",
        )
    for index, upload in enumerate(uploads):
        digest = hashlib.sha256()
        size = 0
        path = os.path.join(workdir, f"upload-{index}")
        with open(path, "wb") as sink:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                total += len(chunk)
                if size > limits.max_file_bytes:
                    raise UploadTooLargeError(
                        f"{upload.filename} is larger than the per-file limit of {limits.max_file_bytes // (1024 * 1024)} MB. "
                        "Compress it into a ZIP, or split the history into several uploads and use append mode.",
                        field="files",
                        details={"limit_bytes": limits.max_file_bytes},
                    )
                if total > limits.max_upload_bytes:
                    raise UploadTooLargeError(
                        f"The upload is larger than the {limits.max_upload_bytes // (1024 * 1024)} MB limit. "
                        "Split it into several uploads and use append mode.",
                        field="files",
                        details={"limit_bytes": limits.max_upload_bytes},
                    )
                digest.update(chunk)
                sink.write(chunk)
        staged.append(StagedFile(upload.filename or f"file-{index}", path, size, digest.hexdigest()))
    return staged


def _parse_config(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ScenarioValidationError("config must be a JSON object.", field="config") from exc
    if not isinstance(value, dict):
        raise ScenarioValidationError("config must be a JSON object.", field="config")
    return value


def _dispatch(service: TrainingDatasetService, dataset: TrainingDataset, factory, response: Response) -> None:
    """Process inline when small; otherwise hand to the background worker."""
    if (dataset.raw_size_bytes or 0) <= service.limits.inline_processing_bytes:
        service.process(dataset)
        return
    service.session.commit()  # the worker reads the row in its own session
    ingestion_jobs.submit(dataset.id, factory)
    response.status_code = 202


@router.post(
    "/datasets",
    response_model=DatasetDetail,
    status_code=201,
    summary="Upload training data: CSV, Parquet, several related files, or a ZIP",
    responses={
        202: {"description": "Accepted; processing continues in the background. Poll the dataset."},
        413: {"description": "Beyond the configured upload limits."},
    },
)
async def upload_dataset(
    request: Request,
    response: Response,
    files: Optional[List[UploadFile]] = File(default=None),
    file: Optional[UploadFile] = File(default=None),
    name: str = Form(..., max_length=80),
    mode: str = Form("independent"),
    currency: Optional[str] = Form(None, max_length=8),
    source_type: str = Form("real"),
    base_dataset_id: Optional[str] = Form(None),
    conflict_policy: Optional[str] = Form(None),
    adapter_id: Optional[str] = Form(None),
    config: Optional[str] = Form(None, max_length=8000),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
    forecasts: ForecastService = Depends(get_forecast_service),
) -> DatasetDetail:
    """
    Store the files write-once under generated keys, then process them.

    Small uploads are processed in the request (201). Larger ones return 202
    with ``status: processing``; poll ``GET /datasets/{id}``. The originals
    are never modified; the normalized table is stored separately.
    """
    limits = UploadLimits.from_env()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limits.max_upload_bytes + CHUNK:
        raise UploadTooLargeError(
            f"The upload is {int(declared):,} bytes; the limit is {limits.max_upload_bytes:,} bytes. "
            "Split it into several uploads and use append mode.",
            field="files",
            details={"limit_bytes": limits.max_upload_bytes, "size_bytes": int(declared)},
        )
    uploads = list(files or []) + ([file] if file is not None else [])
    if not uploads:
        raise ScenarioValidationError("Choose at least one file.", field="files")
    settings = _parse_config(config)
    if currency:
        settings["currency"] = currency
    if adapter_id:
        settings["adapter_id"] = adapter_id

    factory = _service_factory(store, forecasts)
    service = factory(session)
    workdir = tempfile.mkdtemp(prefix="upload-")
    try:
        staged = await _stage(uploads, workdir, limits)
        dataset, created = service.create_session(
            user=user,
            staged=staged,
            name=name,
            mode=mode,
            source_type=source_type,
            config=settings,
            base_dataset_id=base_dataset_id,
            conflict_policy=conflict_policy,
            upload_key=idempotency_key,
        )
    finally:
        # Staging only; the originals are already in object storage.
        shutil.rmtree(workdir, ignore_errors=True)
    if not created:
        response.status_code = 200
        return _dataset_out(dataset, detail=True)
    AuditService(session).record(
        action=AuditAction.DATASET_UPLOADED,
        resource_type=ResourceType.TRAINING_DATASET,
        resource_id=dataset.id.hex,
        actor=actor_for(user, request),
        metadata={
            "name": dataset.name,
            "version": dataset.version,
            "mode": dataset.mode,
            "files": [{k: f.get(k) for k in ("name", "size", "sha256")} for f in dataset.source_files or []],
        },
    )
    _dispatch(service, dataset, factory, response)
    return _dataset_out(dataset, detail=True)


@router.get("/datasets", response_model=DatasetListResponse, summary="Training dataset sessions and versions")
def list_datasets(
    status: Optional[str] = Query(default=None, pattern="^(processing|needs_mapping|invalid|validated|ready|failed)$"),
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
) -> DatasetListResponse:
    return DatasetListResponse(datasets=[_dataset_out(item) for item in _datasets(session, store).list(status=status)])


@router.get("/datasets/{dataset_id}", response_model=DatasetDetail, summary="One dataset session or version")
def get_dataset(
    dataset_id: str,
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
) -> DatasetDetail:
    return _dataset_out(_datasets(session, store).get(dataset_id), detail=True)


@router.get("/datasets/{dataset_id}/report", summary="Download the validation report")
def download_report(
    dataset_id: str,
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
) -> Response:
    dataset = _datasets(session, store).get(dataset_id)
    body = {
        "dataset": json.loads(_dataset_out(dataset).model_dump_json()),
        "validation_report": dataset.validation_report or {},
    }
    filename = f"validation-report-{dataset.id.hex[:8]}-v{dataset.version}.json"
    return Response(
        content=json.dumps(body, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.put(
    "/datasets/{dataset_id}/configuration",
    response_model=DatasetDetail,
    summary="Choose the adapter, confirm the mapping and policies, and re-validate",
)
def configure_dataset(
    dataset_id: str,
    request: Request,
    response: Response,
    payload: DatasetConfigureRequest,
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
    forecasts: ForecastService = Depends(get_forecast_service),
) -> DatasetDetail:
    """Any change invalidates the previous validation; the new result replaces it."""
    factory = _service_factory(store, forecasts)
    service = factory(session)
    dataset = service.configure(service.get(dataset_id), payload.config, payload.expected_mapping_version)
    AuditService(session).record(
        action=AuditAction.DATASET_CONFIGURED,
        resource_type=ResourceType.TRAINING_DATASET,
        resource_id=dataset.id.hex,
        actor=actor_for(user, request),
        metadata={"mapping_version": dataset.mapping_version, "config": dataset.ingestion_config},
    )
    _dispatch(service, dataset, factory, response)
    return _dataset_out(dataset, detail=True)


@router.post("/datasets/{dataset_id}/retry", response_model=DatasetDetail, summary="Retry a failed processing job")
def retry_dataset(
    dataset_id: str,
    response: Response,
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
    forecasts: ForecastService = Depends(get_forecast_service),
) -> DatasetDetail:
    factory = _service_factory(store, forecasts)
    service = factory(session)
    dataset = service.retry(service.get(dataset_id))
    _dispatch(service, dataset, factory, response)
    return _dataset_out(dataset, detail=True)


@router.post(
    "/datasets/{dataset_id}/validate",
    response_model=DatasetDetail,
    summary="Create the immutable version from a validated session",
)
def validate_dataset(
    dataset_id: str,
    request: Request,
    payload: Optional[DatasetSealRequest] = Body(default=None),
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
) -> DatasetDetail:
    """
    Seal a validated session: the manifest is written once and the version is
    immutable. Refused for a stale view (``expected_mapping_version``) and when
    warnings that need acknowledgment are not acknowledged.
    """
    service = _datasets(session, store)
    dataset = service.get(dataset_id)
    was_ready = dataset.status == DatasetStatus.READY.value
    payload = payload or DatasetSealRequest()
    dataset = service.validate_and_seal(
        dataset,
        expected_mapping_version=payload.expected_mapping_version,
        acknowledged=payload.acknowledged_warnings,
    )
    if not was_ready:
        AuditService(session).record(
            action=AuditAction.DATASET_SEALED,
            resource_type=ResourceType.TRAINING_DATASET,
            resource_id=dataset.id.hex,
            actor=actor_for(user, request),
            metadata={
                "name": dataset.name,
                "version": dataset.version,
                "adapter": dataset.adapter_id,
                "mapping_version": dataset.mapping_version,
                "content_sha256": dataset.content_sha256,
                "manifest_sha256": dataset.manifest_sha256,
                "acknowledged_warnings": dataset.acknowledged_warnings,
            },
        )
    return _dataset_out(dataset, detail=True)


@router.get(
    "/datasets/{dataset_id}/history",
    response_model=DatasetHistoryResponse,
    summary="Recent history of one store from a sealed dataset",
)
def dataset_history(
    dataset_id: str,
    store_id: str = Query(..., min_length=1, max_length=64),
    days: int = Query(default=90, ge=7, le=1095),
    user: User = Depends(require_permission(Permission.GENERATE_FORECAST)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
) -> DatasetHistoryResponse:
    """
    Real recorded days from a sealed training dataset, for the Forecast
    Console. Returned as-is: nothing is generated or filled in.
    """
    service = _datasets(session, store)
    dataset = service.get(dataset_id)
    return DatasetHistoryResponse(
        dataset_id=dataset.id.hex,
        dataset_label=f"{dataset.name} v{dataset.version}",
        store_id=store_id,
        history=service.history(dataset, store_id, days),
    )


# ---------------------------------------------------------------------------
# Training runs
# ---------------------------------------------------------------------------


@router.post("/runs", response_model=TrainingRunDetail, summary="Start training on a sealed dataset")
def create_run(
    payload: TrainingRunCreate,
    request: Request,
    response: Response,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_permission(Permission.REQUEST_TRAINING)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
    airflow=Depends(get_airflow_client),
    forecasts: ForecastService = Depends(get_forecast_service),
) -> TrainingRunDetail:
    """
    Record the request, then start the training DAG with server-resolved
    metadata. Returns immediately; poll ``GET /runs/{id}`` for progress.

    Send the same ``Idempotency-Key`` to retry safely: a repeat returns the
    original request instead of starting a second run.
    """
    if not idempotency_key:
        raise ScenarioValidationError("The Idempotency-Key header is required.", field="idempotency_key")
    dataset = _datasets(session, store).get(payload.dataset_id)
    service = TrainingService(session, airflow=airflow)
    row, created = service.submit(
        user=user,
        dataset=dataset,
        config_id=payload.training_config_id,
        idempotency_key=idempotency_key,
    )
    if created:
        AuditService(session).record(
            action=AuditAction.RETRAINING_REQUESTED,
            resource_type=ResourceType.TRAINING_REQUEST,
            resource_id=str(row.id),
            actor=actor_for(user, request),
            outcome="failure" if row.status == TrainingStatus.FAILED.value else "success",
            metadata={
                "dataset_id": dataset.id.hex,
                "dataset": f"{dataset.name} v{dataset.version}",
                "content_sha256": dataset.content_sha256,
                "training_config_id": row.training_config_id,
                "dag_id": row.dag_id,
                "dag_run_id": row.dag_run_id,
                "status": row.status,
            },
        )
    response.status_code = 201 if created else 200
    return _detail(service, row, user, forecasts, sync_warning=None)


@router.get("/runs", response_model=TrainingRunListResponse, summary="Training requests")
def list_runs(
    user: User = Depends(require_permission(Permission.REQUEST_TRAINING)),
    session: Session = Depends(get_session),
    airflow=Depends(get_airflow_client),
) -> TrainingRunListResponse:
    service = TrainingService(session, airflow=airflow)
    rows = service.list()
    for row in [item for item in rows if not TrainingStatus(item.status).is_terminal][:LIST_SYNC_LIMIT]:
        service.sync(row)
    return TrainingRunListResponse(runs=[TrainingRunOut(**_run_fields(row)) for row in rows])


@router.get("/runs/{training_request_id}", response_model=TrainingRunDetail, summary="One training request, refreshed from Airflow")
def get_run(
    training_request_id: str,
    user: User = Depends(require_permission(Permission.REQUEST_TRAINING)),
    session: Session = Depends(get_session),
    airflow=Depends(get_airflow_client),
    forecasts: ForecastService = Depends(get_forecast_service),
) -> TrainingRunDetail:
    service = TrainingService(session, airflow=airflow)
    row = service.get(training_request_id)
    warning = service.sync(row)
    return _detail(service, row, user, forecasts, sync_warning=warning)


@router.post(
    "/runs/{training_request_id}/reject",
    response_model=TrainingRunDetail,
    summary="Reject a candidate (deployment goes through /v1/models/{version}/promote)",
)
def reject_candidate(
    training_request_id: str,
    payload: RejectCandidateRequest,
    request: Request,
    user: User = Depends(require_permission(Permission.PROMOTE_MODEL)),
    session: Session = Depends(get_session),
    airflow=Depends(get_airflow_client),
    forecasts: ForecastService = Depends(get_forecast_service),
) -> TrainingRunDetail:
    service = TrainingService(session, airflow=airflow)
    row = service.get(training_request_id)
    service.candidate(row, payload.model, payload.version)
    if row.deployed_model == payload.model and row.deployed_version == payload.version:
        raise ScenarioValidationError(
            "This candidate is deployed; roll back from the Model Registry instead.", field="version"
        )
    service.record_decision(
        row=row,
        user=user,
        kind=DecisionKind.REJECTED,
        model=payload.model,
        version=payload.version,
        reason=payload.reason.strip(),
        checks={},
    )
    AuditService(session).record(
        action=AuditAction.CANDIDATE_REJECTED,
        resource_type=ResourceType.MODEL_VERSION,
        resource_id=f"{payload.model}:{payload.version}",
        actor=actor_for(user, request),
        metadata={"training_request_id": str(row.id), "reason": payload.reason.strip()},
    )
    return _detail(service, row, user, forecasts, sync_warning=None)


def _detail(
    service: TrainingService,
    row: TrainingRequest,
    user: User,
    forecasts: ForecastService,
    sync_warning: Optional[str],
) -> TrainingRunDetail:
    can_see_airflow = has_permission(user.role_enum, Permission.TRIGGER_RETRAINING)
    return TrainingRunDetail(
        **_run_fields(row),
        task_states=list(row.task_states or []),
        evaluation=row.evaluation or {},
        decisions=[
            DecisionOut(
                decision=item.decision,
                model=item.model,
                version=item.version,
                reason=item.reason,
                previous_production_version=item.previous_production_version,
                checks=item.checks or {},
                decided_at=item.decided_at,
                decided_by=item.decided_by_email,
                decided_by_role=item.decided_by_role,
            )
            for item in service.decisions(row)
        ],
        production=_production(forecasts),
        airflow_url=service.run_url(row) if can_see_airflow else None,
        mlflow_url=mlflow_browse_url(row.mlflow_run_id),
        sync_warning=sync_warning,
        can_deploy=has_permission(user.role_enum, Permission.PROMOTE_MODEL),
    )


__all__ = ["TAG", "router"]
