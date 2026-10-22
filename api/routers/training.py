"""
Training workspace: dataset versions and training requests.

Three workflows in this platform accept sales data, and they are kept apart by
route, permission, and record:

* ``/v1/training/datasets``  -- historical data to *train* a model on
  (this router; persisted as immutable dataset versions);
* ``POST /v1/forecast``      -- history for *one* forecast, never stored;
* monitoring actuals         -- observed outcomes scored against forecasts
  (the ``actuals`` table, loaded by the monitoring DAG).

Deployment is not here. A candidate is deployed through the existing
``POST /v1/models/{version}/promote``, which runs the candidate gates; this
router only records rejections.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from api.core.errors import ScenarioValidationError, UploadTooLargeError
from api.core.rbac import Permission, has_permission
from api.db.models.audit import AuditAction, ResourceType
from api.db.models.training import DecisionKind, TrainingDataset, TrainingRequest, TrainingStatus
from api.db.models.user import User
from api.db.session import get_session
from api.dependencies.auth import actor_for, require_permission
from api.deps import get_airflow_client, get_dataset_store, get_forecast_service
from api.routers.models import mlflow_browse_url
from api.schemas.training import (
    DatasetDetail,
    DatasetHistoryResponse,
    DatasetListResponse,
    DatasetOut,
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
from api.services.audit_service import AuditService
from api.services.forecast_service import ForecastService
from api.services.training_dataset_service import TrainingDatasetService
from api.services.training_service import TrainingService
from training import contract, storage
from training.configs import TRAINING_CONFIGS

logger = logging.getLogger(__name__)

TAG = "Training"
router = APIRouter(prefix="/v1/training", tags=[TAG])

#: Non-terminal runs refreshed from Airflow on a list read.
LIST_SYNC_LIMIT = 10

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


def _datasets(session: Session, store) -> TrainingDatasetService:
    return TrainingDatasetService(session, store=store)


def _dataset_out(dataset: TrainingDataset, detail: bool = False):
    report = dataset.validation_report or {}
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
        currency=dataset.currency,
        frequency=dataset.frequency,
        source_types=list(dataset.source_types or []),
        original_filename=dataset.original_filename,
        file_format=dataset.file_format,
        raw_sha256=dataset.raw_sha256,
        raw_size_bytes=dataset.raw_size_bytes,
        content_sha256=dataset.content_sha256,
        manifest_sha256=dataset.manifest_sha256,
        manifest_uri=storage.to_uri(dataset.manifest_object_key) if dataset.manifest_object_key else None,
        date_min=dataset.date_min,
        date_max=dataset.date_max,
        row_count=dataset.row_count,
        store_count=dataset.store_count,
        stores=list(dataset.stores or []),
        created_at=dataset.created_at,
        created_by=dataset.created_by_email,
        sealed_at=dataset.sealed_at,
        error_count=len(report.get("errors") or []),
        warning_count=len(report.get("warnings") or []),
    )
    if detail:
        return DatasetDetail(**data, validation_report=report)
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


@router.get("/contract", response_model=TrainingContractResponse, summary="Accepted training data and configurations")
def get_contract(
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
) -> TrainingContractResponse:
    """The accepted schema, the validation policy, and the approved configurations."""
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
    )


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


@router.post(
    "/datasets",
    response_model=DatasetDetail,
    status_code=201,
    summary="Upload training data (CSV or Parquet)",
    responses={413: {"description": "The file exceeds the size limit."}},
)
async def upload_dataset(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(..., max_length=80),
    mode: str = Form("independent"),
    currency: str = Form("USD", max_length=3),
    source_type: str = Form("real"),
    base_dataset_id: Optional[str] = Form(None),
    conflict_policy: Optional[str] = Form(None),
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
) -> DatasetDetail:
    """
    Store the upload under a generated key and validate it.

    The response carries the full validation report. A valid upload is a
    ``validated`` draft; ``POST /datasets/{id}/validate`` seals it into an
    immutable version that training can use.
    """
    service = _datasets(session, store)
    limit = service.policy.max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 1024 * 1024:
        raise UploadTooLargeError(f"The upload exceeds the {limit:,}-byte limit.", field="file")
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise UploadTooLargeError(f"The file exceeds the {limit:,}-byte limit.", field="file")

    dataset = service.upload(
        user=user,
        filename=file.filename,
        content=content,
        name=name,
        mode=mode,
        currency=currency,
        source_type=source_type,
        base_dataset_id=base_dataset_id,
        conflict_policy=conflict_policy,
    )
    AuditService(session).record(
        action=AuditAction.DATASET_UPLOADED,
        resource_type=ResourceType.TRAINING_DATASET,
        resource_id=dataset.id.hex,
        actor=actor_for(user, request),
        outcome="success" if dataset.status != "invalid" else "failure",
        metadata={
            "name": dataset.name,
            "version": dataset.version,
            "mode": dataset.mode,
            "status": dataset.status,
            "raw_sha256": dataset.raw_sha256,
            "rows": (dataset.validation_report or {}).get("rows_received"),
        },
    )
    return _dataset_out(dataset, detail=True)


@router.get("/datasets", response_model=DatasetListResponse, summary="Training dataset versions")
def list_datasets(
    status: Optional[str] = Query(default=None, pattern="^(invalid|validated|ready)$"),
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
) -> DatasetListResponse:
    return DatasetListResponse(
        datasets=[_dataset_out(item) for item in _datasets(session, store).list(status=status)]
    )


@router.get("/datasets/{dataset_id}", response_model=DatasetDetail, summary="One dataset version")
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


@router.post(
    "/datasets/{dataset_id}/validate",
    response_model=DatasetDetail,
    summary="Re-validate from storage and seal an immutable version",
)
def validate_dataset(
    dataset_id: str,
    request: Request,
    user: User = Depends(require_permission(Permission.MANAGE_TRAINING_DATA)),
    session: Session = Depends(get_session),
    store=Depends(get_dataset_store),
) -> DatasetDetail:
    service = _datasets(session, store)
    dataset = service.get(dataset_id)
    was_ready = dataset.status == "ready"
    dataset = service.validate_and_seal(dataset)
    if not was_ready:
        AuditService(session).record(
            action=AuditAction.DATASET_SEALED,
            resource_type=ResourceType.TRAINING_DATASET,
            resource_id=dataset.id.hex,
            actor=actor_for(user, request),
            outcome="success" if dataset.status == "ready" else "failure",
            metadata={
                "name": dataset.name,
                "version": dataset.version,
                "status": dataset.status,
                "content_sha256": dataset.content_sha256,
                "manifest_sha256": dataset.manifest_sha256,
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
