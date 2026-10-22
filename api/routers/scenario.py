"""
Scenario Planner HTTP routes.

Handlers are deliberately thin: they read headers, delegate to services, and
return schema objects. Errors are raised, never caught-and-shaped here -- the
central error handler renders every failure into the one documented envelope.

Two route-shaped schemas (the rollback request body and the audit list response)
are defined here because they describe this transport only and have no meaning
to the services.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from api.config import get_settings
from api.core.errors import (
    BaselineNotFoundError,
    ScenarioValidationError,
    ScenarioExpiredError,
    ScenarioNotFoundError,
    ScenarioNotSupportedError,
)
from api.core.rbac import Permission
from api.db.models.user import User
from api.db.session import get_session
from api.db.models.audit import AuditAction, ResourceType
from api.services.audit_service import OUTCOME_FAILURE, AuditService
from api.dependencies.auth import actor_for, require_permission
from api.dependencies.scenario import (
    get_audit_service,
    get_capability_service,
    get_checkpoint_service,
    get_forecast_service,
    get_rollback_service,
)
from api.middleware.error_handler import REQUEST_ID_HEADER, get_request_id, new_request_id
from api.observability.scenario_logging import (
    ScenarioLogContext,
    StageTimer,
    log_capability_check_completed,
    log_request_received,
    log_scenario_completed,
    log_scenario_failed,
    log_scenario_rolled_back,
    log_validation_completed,
)
from api.observability.scenario_metrics import get_metrics
from api.schemas.baseline_forecast import BaselineForecastCheckpoint
from api.schemas.scenario import DEFAULT_MODEL_VERSION, ScenarioRequest
from api.schemas.scenario_capabilities import ScenarioCapabilitiesResponse
from api.schemas.scenario_comparison import ScenarioComparisonResponse
from api.services.scenario_audit_service import (
    ScenarioAuditEvent,
    ScenarioAuditService,
    ScenarioAuditStatus,
    compute_request_hash,
)
from api.services.scenario_capability_service import ScenarioCapabilityService
from api.services.scenario_forecast_service import ScenarioForecastService
from api.services.scenario_rollback_service import (
    RollbackResponse,
    ScenarioLockRegistry,
    ScenarioRollbackService,
    ScenarioStage,
)

logger = logging.getLogger(__name__)

TAG = "Scenario Planning"

router = APIRouter(prefix="/v1/scenario", tags=[TAG])


# --------------------------------------------------------------------------
# Route-local schemas
# --------------------------------------------------------------------------


class ScenarioRollbackRequest(BaseModel):
    """Body for a rollback request."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=3,
        max_length=1000,
        description="Why the scenario is being rolled back; recorded in the audit trail.",
    )


class ScenarioAuditEventResponse(BaseModel):
    """One audit event, without internal identifiers."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    request_id: str
    scenario_id: Optional[str] = None
    baseline_forecast_id: Optional[str] = None
    store_id: str
    model_version: str
    scenario_type: str
    status: ScenarioAuditStatus
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime

    @classmethod
    def from_event(cls, event: ScenarioAuditEvent) -> "ScenarioAuditEventResponse":
        """Project an ORM row onto the transport schema.

        The stored ``request_payload`` and ``response_summary`` are deliberately
        omitted: they can be large and may echo the caller's full history.
        """
        return cls(
            request_id=event.request_id,
            scenario_id=event.scenario_id,
            baseline_forecast_id=event.baseline_forecast_id,
            store_id=event.store_id,
            model_version=event.model_version,
            scenario_type=event.scenario_type,
            status=ScenarioAuditStatus(event.status),
            error_code=event.error_code,
            error_message=event.error_message,
            duration_ms=event.duration_ms,
            created_at=event.created_at,
        )


class ScenarioAuditListResponse(BaseModel):
    """Audit trail for one scenario."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    event_count: int = Field(ge=0)
    events: List[ScenarioAuditEventResponse] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get(
    "/capabilities",
    response_model=ScenarioCapabilitiesResponse,
    summary="Scenario types the selected model can support",
    responses={
        404: {"description": "Model version not found"},
        409: {"description": "Model has no feature schema"},
        503: {"description": "MLflow unavailable"},
    },
)
def get_capabilities(
    model_version: str = Query(
        default=DEFAULT_MODEL_VERSION,
        description="Registry version, or 'latest' for the current Production model.",
    ),
    model: Optional[str] = Query(
        default=None,
        description="Model family (ensemble, xgboost, lightgbm); omitted means the default.",
    ),
    capability_service: ScenarioCapabilityService = Depends(get_capability_service),
    user: User = Depends(require_permission(Permission.VIEW_SCENARIO)),
) -> ScenarioCapabilitiesResponse:
    """
    Report which scenario types this model version supports, and why not.

    Read-only, so every role may call it: the planner has to know what is
    possible before it can disable what is not.
    """
    return capability_service.get_capabilities(model_version, **_model_kwargs(model))


@router.get(
    "/baseline/{baseline_forecast_id}",
    response_model=BaselineForecastCheckpoint,
    summary="Baseline forecast metadata",
    responses={404: {"description": "Baseline not found"}},
)
def get_baseline(
    baseline_forecast_id: str = Path(description="Identifier returned by a comparison."),
    checkpoint_service=Depends(get_checkpoint_service),
    user: User = Depends(require_permission(Permission.VIEW_SCENARIO)),
) -> BaselineForecastCheckpoint:
    """
    Return a baseline checkpoint.

    Expired baselines are returned rather than hidden: the row carries its own
    ``status`` and ``expires_at``, and being able to inspect a stale baseline is
    what makes a failed reuse diagnosable.
    """
    checkpoint = checkpoint_service.get(baseline_forecast_id)
    if checkpoint is None:
        raise BaselineNotFoundError(
            field="baseline_forecast_id",
            details={"baseline_forecast_id": baseline_forecast_id},
        )
    return checkpoint


@router.post(
    "/compare",
    response_model=ScenarioComparisonResponse,
    summary="Compare a what-if scenario against its baseline",
    responses={
        404: {"description": "Model or baseline not found"},
        409: {"description": "Unsupported scenario, or idempotency key conflict"},
        422: {"description": "Request validation failed"},
        500: {"description": "Prediction or internal error"},
        503: {"description": "MLflow or database unavailable"},
    },
)
def compare_scenario(
    request: ScenarioRequest,
    response: Response,
    http_request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    forecast_service: ScenarioForecastService = Depends(get_forecast_service),
    capability_service: ScenarioCapabilityService = Depends(get_capability_service),
    audit_service: ScenarioAuditService = Depends(get_audit_service),
    rollback_service: ScenarioRollbackService = Depends(get_rollback_service),
    user: User = Depends(require_permission(Permission.RUN_SCENARIO)),
    session: Session = Depends(get_session),
) -> ScenarioComparisonResponse:
    """
    Run a what-if comparison.

    Replays a previous result when the same ``Idempotency-Key`` is reused with
    an identical payload, and conflicts (409) when it is reused with a different
    one. Every attempt -- successful or not -- lands in the scenario audit
    trail, and the comparison is additionally recorded in the platform-wide
    audit log so "who ran what" is answerable from one place.
    """
    request_id = get_request_id() or new_request_id()
    response.headers[REQUEST_ID_HEADER] = request_id
    actor = actor_for(user, http_request)

    try:
        comparison = _run_comparison(
            request=request,
            request_id=request_id,
            idempotency_key=idempotency_key,
            forecast_service=forecast_service,
            capability_service=capability_service,
            audit_service=audit_service,
            rollback_service=rollback_service,
        )
    except Exception as exc:
        # A refused or failed scenario is as worth recording as a successful
        # one: a pattern of failures is how an unsupported model is noticed.
        AuditService(session).record(
            action=AuditAction.SCENARIO_COMPARED,
            resource_type=ResourceType.SCENARIO,
            actor=actor,
            outcome=OUTCOME_FAILURE,
            metadata={
                "store_id": request.store_id,
                "scenario_type": request.scenario.scenario_type.value,
                "error": type(exc).__name__,
            },
        )
        raise

    AuditService(session).record(
        action=AuditAction.SCENARIO_COMPARED,
        resource_type=ResourceType.SCENARIO,
        resource_id=comparison.scenario_id,
        actor=actor,
        metadata={
            "store_id": comparison.store_id,
            "scenario_type": request.scenario.scenario_type.value,
            "model": comparison.model,
            "model_version": comparison.model_version,
            "horizon_days": request.horizon_days,
            "baseline_forecast_id": comparison.baseline_forecast_id,
        },
    )
    _attribute_scenario(session, comparison.scenario_id, user)
    return comparison


@router.get(
    "/{scenario_id}",
    response_model=ScenarioComparisonResponse,
    summary="Retrieve a stored scenario comparison",
    responses={
        404: {"description": "Scenario not found"},
        410: {"description": "Scenario result has expired"},
    },
)
def get_scenario(
    scenario_id: str = Path(description="Identifier returned by a comparison."),
    audit_service: ScenarioAuditService = Depends(get_audit_service),
    checkpoint_service=Depends(get_checkpoint_service),
    user: User = Depends(require_permission(Permission.VIEW_SCENARIO)),
) -> ScenarioComparisonResponse:
    """
    Return a previously computed comparison.

    Comparisons are stored as the ``response_summary`` of their completed audit
    event. Expiry is inherited from the baseline the comparison was measured
    against: once that baseline expires the stored result can no longer be
    reproduced or trusted, so the scenario reads as 410 Gone.
    """
    events = audit_service.get_by_scenario_id(scenario_id)
    if not events:
        raise _scenario_not_found(scenario_id)

    completed = next(
        (
            event
            for event in reversed(events)
            if event.status == ScenarioAuditStatus.SCENARIO_COMPLETED.value
            and event.response_summary
        ),
        None,
    )
    if completed is None:
        raise _scenario_not_found(
            scenario_id,
            detail="No completed comparison is stored for this scenario.",
            statuses=[event.status for event in events],
        )

    _assert_not_expired(scenario_id, completed, checkpoint_service)
    return ScenarioComparisonResponse.model_validate(completed.response_summary)


@router.post(
    "/{scenario_id}/rollback",
    response_model=RollbackResponse,
    summary="Roll back a scenario, preserving its audit history",
    responses={503: {"description": "Database unavailable"}},
)
def rollback_scenario(
    scenario_id: str = Path(description="Scenario to roll back."),
    body: ScenarioRollbackRequest = Body(...),
    rollback_service: ScenarioRollbackService = Depends(get_rollback_service),
    user: User = Depends(require_permission(Permission.RUN_SCENARIO)),
) -> RollbackResponse:
    """
    Mark a scenario rolled back.

    Nothing is deleted: the rollback is appended to the audit trail and the
    baseline forecast is left intact.
    """
    timer = StageTimer()
    result = rollback_service.rollback_scenario(
        scenario_id=scenario_id,
        reason=body.reason,
        request_id=get_request_id(),
    )

    get_metrics().record_rollback("succeeded" if result.rolled_back else "failed")
    log_scenario_rolled_back(
        ScenarioLogContext(
            request_id=result.request_id or get_request_id() or "unknown",
            store_id="unknown",
            model_version="unknown",
            scenario_type="unknown",
            scenario_id=scenario_id,
            baseline_forecast_id=result.baseline_forecast_id,
        ),
        reason=body.reason,
        duration_ms=timer.elapsed_ms(),
        audit_preserved=result.audit_history_preserved,
        baseline_preserved=result.baseline_preserved,
    )
    return result


@router.get(
    "/{scenario_id}/audit",
    response_model=ScenarioAuditListResponse,
    summary="Audit trail for a scenario",
    responses={404: {"description": "Scenario not found"}},
)
def get_scenario_audit(
    scenario_id: str = Path(description="Scenario whose history to return."),
    audit_service: ScenarioAuditService = Depends(get_audit_service),
    user: User = Depends(require_permission(Permission.VIEW_AUDIT_LOGS)),
) -> ScenarioAuditListResponse:
    """
    Return every audit event recorded for a scenario, oldest first.

    Requires ``VIEW_AUDIT_LOGS`` (ADMIN or ML_ENGINEER). This replaces the
    earlier placeholder, which read a role from a client-supplied header and
    could therefore be set by any caller.
    """
    events = audit_service.get_by_scenario_id(scenario_id)
    if not events:
        raise _scenario_not_found(scenario_id)

    return ScenarioAuditListResponse(
        scenario_id=scenario_id,
        event_count=len(events),
        events=[ScenarioAuditEventResponse.from_event(event) for event in events],
    )


# --------------------------------------------------------------------------
# Orchestration helper
# --------------------------------------------------------------------------


def _run_comparison(
    *,
    request: ScenarioRequest,
    request_id: str,
    idempotency_key: Optional[str],
    forecast_service: ScenarioForecastService,
    capability_service: ScenarioCapabilityService,
    audit_service: ScenarioAuditService,
    rollback_service: ScenarioRollbackService,
) -> ScenarioComparisonResponse:
    """
    Sequence the comparison: idempotency, capability, forecast, audit.

    Lives beside the route rather than inside a service because it is pure glue
    between five services. A dedicated orchestration service would be the
    natural home once a second caller needs the same sequence.
    """
    payload = request.model_dump(mode="json")
    scenario_type = request.scenario.scenario_type.value
    model_version = request.model_version or DEFAULT_MODEL_VERSION
    metrics = get_metrics()

    log_context = ScenarioLogContext(
        request_id=request_id,
        store_id=request.store_id,
        model_version=model_version,
        scenario_type=scenario_type,
        baseline_forecast_id=request.baseline_forecast_id,
    )
    # The payload itself is never logged -- it carries the caller's sales
    # history. Only its shape is.
    log_request_received(
        log_context,
        horizon_days=request.horizon_days,
        history_rows=len(request.history),
        idempotency_key_present=bool(idempotency_key),
    )
    # Pydantic validated the request before this handler ran, so reaching here
    # means validation passed.
    log_validation_completed(log_context, duration_ms=0, ok=True)

    # Idempotent replay happens before anything is recorded or computed.
    replay = audit_service.resolve_idempotent_request(idempotency_key, payload)
    if replay:
        logger.info("Replaying idempotent scenario for key=%s", idempotency_key)
        metrics.record_request(scenario_type, "replayed")
        return ScenarioComparisonResponse.model_validate(replay)

    audit_service.record_safely(
        request_id=request_id,
        store_id=request.store_id,
        model_version=model_version,
        scenario_type=scenario_type,
        status=ScenarioAuditStatus.RECEIVED,
        request_payload=payload,
        baseline_forecast_id=request.baseline_forecast_id,
        idempotency_key=idempotency_key,
    )

    failure_context: Dict[str, Any] = {
        "request_id": request_id,
        "store_id": request.store_id,
        "model_version": model_version,
        "scenario_type": scenario_type,
        "request_payload": payload,
        "baseline_forecast_id": request.baseline_forecast_id,
        "lock_key": ScenarioLockRegistry.build_key(
            request.store_id, model_version, scenario_type
        ),
    }
    started = time.perf_counter()
    stage_clock = StageTimer()

    try:
        _assert_scenario_supported(request, capability_service)
    except Exception as exc:
        metrics.record_capability_check(scenario_type, supported=False)
        log_capability_check_completed(
            log_context,
            duration_ms=stage_clock.elapsed_ms(),
            supported=False,
            missing_features=(getattr(exc, "details", {}) or {}).get("missing_features"),
            error_code=getattr(exc, "error_code", None),
        )
        _record_failure(metrics, log_context, exc, scenario_type, "capability_check", started)
        rollback_service.handle_failure(
            stage=ScenarioStage.CAPABILITY_CHECK, exc=exc, **failure_context
        )
        raise

    metrics.record_capability_check(scenario_type, supported=True)
    log_capability_check_completed(
        log_context, duration_ms=stage_clock.elapsed_ms(), supported=True
    )

    try:
        comparison = forecast_service.create_comparison(request, log_context=log_context)
    except Exception as exc:
        stage = _failure_stage(exc)
        _record_failure(metrics, log_context, exc, scenario_type, stage.value, started)
        rollback_service.handle_failure(
            stage=stage,
            exc=exc,
            duration_ms=_elapsed_ms(started),
            **failure_context,
        )
        raise

    log_context = log_context.with_scenario_id(comparison.scenario_id)
    log_context.baseline_forecast_id = comparison.baseline_forecast_id
    metrics.record_baseline(reused=comparison.baseline_reused)

    try:
        audit_service.record(
            request_id=request_id,
            store_id=request.store_id,
            model_version=comparison.model_version,
            scenario_type=scenario_type,
            status=ScenarioAuditStatus.SCENARIO_COMPLETED,
            request_payload=payload,
            scenario_id=comparison.scenario_id,
            baseline_forecast_id=comparison.baseline_forecast_id,
            response_summary=comparison.model_dump(mode="json"),
            duration_ms=_elapsed_ms(started),
            idempotency_key=idempotency_key,
            request_hash=compute_request_hash(payload) if idempotency_key else None,
        )
    except Exception as exc:
        # The forecast succeeded but could not be recorded. Partial results are
        # not returned: a comparison the caller could never retrieve again, and
        # that no audit trail covers, is worse than a clean failure.
        _record_failure(metrics, log_context, exc, scenario_type, "persistence", started)
        rollback_service.handle_failure(
            stage=ScenarioStage.PERSISTENCE,
            exc=exc,
            scenario_id=comparison.scenario_id,
            duration_ms=_elapsed_ms(started),
            **failure_context,
        )
        raise

    duration_ms = _elapsed_ms(started)
    metrics.record_request(scenario_type, "completed")
    metrics.record_duration(scenario_type, duration_ms / 1000)
    log_scenario_completed(
        log_context,
        duration_ms=duration_ms,
        baseline_reused=comparison.baseline_reused,
        partial=bool(comparison.unsupported_overrides),
    )
    return comparison


def _record_failure(
    metrics: Any,
    log_context: ScenarioLogContext,
    exc: BaseException,
    scenario_type: str,
    stage: str,
    started: float,
) -> None:
    """
    Record a failed comparison once, in metrics and logs.

    Latency is recorded for failures too: a timeout that is never measured
    hides exactly the problem an operator needs to see.
    """
    duration_ms = _elapsed_ms(started)
    error_code = getattr(exc, "error_code", type(exc).__name__)
    metrics.record_request(scenario_type, "failed")
    metrics.record_failure(scenario_type, str(error_code), stage=stage)
    metrics.record_duration(scenario_type, duration_ms / 1000)
    log_scenario_failed(log_context, exc, duration_ms=duration_ms, stage=stage)


def _checked_model(model: Optional[str]) -> Optional[str]:
    """
    A requested model family, refused when this deployment does not serve it.

    Refused rather than mapped to the default: a planner who asked for XGBoost
    and silently got the ensemble would read the wrong model's answer.
    """
    if model is None:
        return None
    available = list(get_settings().model_names)
    if model not in available:
        raise ScenarioValidationError(
            f"Model '{model}' is not one this deployment serves.",
            field="model",
            details={"model": model, "available_models": available},
        )
    return model


def _model_kwargs(model: Optional[str]) -> Dict[str, str]:
    """``model=`` for the capability service, only when a family was requested."""
    checked = _checked_model(model)
    return {"model": checked} if checked else {}


def _assert_scenario_supported(
    request: ScenarioRequest, capability_service: ScenarioCapabilityService
) -> None:
    """Reject a scenario the selected model cannot express."""
    requested_overrides = list(request.scenario.custom_overrides or {}) or None
    capabilities = capability_service.get_capabilities(
        request.model_version or DEFAULT_MODEL_VERSION,
        requested_overrides=requested_overrides,
        **_model_kwargs(request.model),
    )

    scenario_type = request.scenario.scenario_type
    if capabilities.is_supported(scenario_type):
        return

    capability = capabilities.capability_for(scenario_type)
    raise ScenarioNotSupportedError(
        (capability.reason if capability and capability.reason else None)
        or f"Scenario type '{scenario_type.value}' is not supported by this model.",
        field="scenario.scenario_type",
        details={
            "scenario_type": scenario_type.value,
            "model_version": capabilities.model_version,
            "missing_features": capability.missing_features if capability else [],
            "supported_scenario_types": [
                item.value for item in capabilities.supported_scenario_types
            ],
        },
    )


def _failure_stage(exc: BaseException) -> ScenarioStage:
    """
    Classify where a comparison failure happened.

    Drives which rollback is applied, and therefore whether a completed
    baseline survives.
    """
    code = getattr(exc, "error_code", "") or ""

    if code in ("SCENARIO_NOT_SUPPORTED", "FEATURE_NOT_AVAILABLE"):
        return ScenarioStage.CAPABILITY_CHECK
    if code in (
        "MODEL_VERSION_NOT_FOUND",
        "MODEL_NOT_FOUND",
        "MODEL_NOT_IN_PRODUCTION",
        "MLFLOW_UNAVAILABLE",
        "FEATURE_SCHEMA_MISSING",
        "INSUFFICIENT_HISTORY",
    ):
        return ScenarioStage.BASELINE_CREATION
    if code in (
        "INVALID_DATE_RANGE",
        "HISTORICAL_DATA_MODIFICATION_ATTEMPTED",
        "TARGET_COLUMN_MODIFICATION_ATTEMPTED",
        "PROTECTED_FEATURE_MODIFICATION_ATTEMPTED",
        "FEATURE_MATRIX_UNAVAILABLE",
    ):
        return ScenarioStage.FEATURE_PREPARATION
    if code in ("DATABASE_UNAVAILABLE", "DATABASE_NOT_CONFIGURED"):
        return ScenarioStage.PERSISTENCE
    return ScenarioStage.PREDICTION


def _assert_not_expired(
    scenario_id: str, event: ScenarioAuditEvent, checkpoint_service: Any
) -> None:
    """Raise 410 when the comparison's baseline has expired."""
    if not event.baseline_forecast_id:
        return

    checkpoint = checkpoint_service.get(event.baseline_forecast_id)
    if checkpoint is None or not checkpoint.is_expired:
        return

    raise ScenarioExpiredError(
        "The stored scenario comparison has expired because its baseline forecast "
        "is no longer valid.",
        field="scenario_id",
        details={
            "scenario_id": scenario_id,
            "baseline_forecast_id": event.baseline_forecast_id,
        },
    )


def _scenario_not_found(
    scenario_id: str,
    detail: Optional[str] = None,
    statuses: Optional[List[str]] = None,
) -> ScenarioNotFoundError:
    """404 for an unknown scenario id."""
    details: Dict[str, Any] = {"scenario_id": scenario_id}
    if statuses:
        details["recorded_statuses"] = statuses
    return ScenarioNotFoundError(
        detail or f"Scenario '{scenario_id}' was not found.",
        field="scenario_id",
        details=details,
    )


def _elapsed_ms(started: float) -> int:
    """Milliseconds since ``started``."""
    return int((time.perf_counter() - started) * 1000)


def _attribute_scenario(session: Session, scenario_id: str, user: User) -> None:
    """
    Stamp the saved scenario row with who created it.

    Done here rather than inside the comparison service so that service stays
    free of any dependency on the authentication layer. Best-effort: a
    persisted comparison without attribution is far better than losing the
    comparison because an attribution write failed.
    """
    try:
        from sqlalchemy import select

        from api.db.models.scenario import ScenarioForecast

        row = session.execute(
            select(ScenarioForecast).where(
                ScenarioForecast.scenario_id == scenario_id
            )
        ).scalar_one_or_none()
        if row is not None and row.created_by_id is None:
            row.created_by_id = user.id
            row.created_by_email = user.email
            session.flush()
    except Exception as exc:  # noqa: BLE001 - attribution must not fail a result
        logger.warning("Could not attribute scenario %s: %s", scenario_id, exc)
