"""
Monitoring endpoints.

Reads are gated on ``VIEW_MONITORING``, which every role holds; the *detail*
endpoints additionally require ``VIEW_MONITORING_DETAIL``, so a BUSINESS_USER
or VIEWER sees the health summary but not per-feature drift scores. Alert
mutations require ``MANAGE_ALERTS`` and are audited.

Every endpoint can answer "there is no data yet" without pretending otherwise.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from api.core.rbac import Permission, has_permission
from api.db.models.audit import AuditAction, ResourceType
from api.db.models.monitoring import AlertStatus, DriftStatus
from api.db.models.user import User
from api.db.session import get_session
from api.dependencies.auth import actor_for, require_permission
from api.deps import get_model_registry_service
from api.schemas.monitoring_evaluate import EvaluateRequest, EvaluateResponse
from api.schemas.monitoring import (
    NO_DATA,
    AccuracyMetrics,
    AccuracyResponse,
    AlertListResponse,
    AlertResponse,
    DriftResponse,
    DriftRow,
    FreshnessResponse,
    MonitoringSummaryResponse,
    ResolveAlertRequest,
    RetrainingRecommendationResponse,
    StoreCoverage,
)
from api.services.audit_service import AuditService
from api.services.model_registry_service import ModelRegistryService
from api.services.monitoring_metrics import METRIC_BIAS, METRIC_MAE, METRIC_WAPE
from api.services.monitoring_service import (
    FreshnessReport,
    MonitoringService,
    RetrainingRecommendation,
    WindowAccuracy,
    monitoring_window_days,
)

logger = logging.getLogger(__name__)

TAG = "Monitoring"

router = APIRouter(prefix="/v1/monitoring", tags=[TAG])


def _freshness_response(report: FreshnessReport) -> FreshnessResponse:
    """Project a freshness report."""
    return FreshnessResponse(
        status=report.status,
        has_data=report.has_data,
        latest_actual_date=report.latest_actual_date,
        latest_forecast_date=report.latest_forecast_date,
        latest_forecast_created_at=report.latest_forecast_created_at,
        days_since_actual=report.days_since_actual,
        actual_count=report.actual_count,
        forecast_count=report.forecast_count,
        coverage=[StoreCoverage(**row) for row in report.coverage],
        no_data_message=None if report.has_data else NO_DATA,
    )


def _accuracy_metrics(result) -> AccuracyMetrics:
    """Project an accuracy result."""
    return AccuracyMetrics(
        sample_size=result.sample_size,
        wape=result.wape,
        mae=result.mae,
        rmse=result.rmse,
        bias=result.bias,
        mean_error_pct=result.mean_error_pct,
        mape=result.mape,
        interval_coverage=result.interval_coverage,
        r2=result.r2,
    )


def _accuracy_response(
    accuracy: WindowAccuracy,
    *,
    window_days: int,
    model_version: Optional[str],
    store_id: Optional[str] = None,
    series: Optional[List[dict]] = None,
) -> AccuracyResponse:
    """Project a window accuracy, including its trend."""
    return AccuracyResponse(
        has_data=accuracy.has_data,
        window_days=window_days,
        window_start=accuracy.window_start,
        window_end=accuracy.window_end,
        model_version=model_version,
        store_id=store_id,
        current=_accuracy_metrics(accuracy.result),
        previous=(
            _accuracy_metrics(accuracy.previous) if accuracy.previous else None
        ),
        trend={
            metric: accuracy.trend(metric)
            for metric in (METRIC_WAPE, METRIC_MAE, METRIC_BIAS)
        },
        series=list(series or []),
        no_data_message=None if accuracy.has_data else NO_DATA,
    )


def _retraining_response(
    recommendation: RetrainingRecommendation,
) -> RetrainingRecommendationResponse:
    """Project a retraining recommendation."""
    return RetrainingRecommendationResponse(
        recommendation=recommendation.recommendation,
        reason=recommendation.reason,
        model_version=recommendation.model_version,
        last_training_date=recommendation.last_training_date,
        wape=recommendation.wape,
        wape_trend=recommendation.wape_trend,
        drift_features=recommendation.drift_features,
    )


def _production_model(registry: ModelRegistryService) -> Optional[dict]:
    """
    The serving model's summary, or ``None``.

    Never raises: the monitoring page must still render when MLflow is down,
    showing what it does know rather than failing wholesale.
    """
    try:
        return registry.production_summary()
    except Exception as exc:  # noqa: BLE001 - monitoring degrades, never fails
        logger.info("Production model summary unavailable for monitoring: %s", exc)
        return None


@router.get(
    "/summary",
    response_model=MonitoringSummaryResponse,
    summary="Monitoring health summary",
)
def summary(
    window_days: int = Query(default=0, ge=0, le=365),
    model_version: Optional[str] = Query(default=None),
    user: User = Depends(require_permission(Permission.VIEW_MONITORING)),
    session: Session = Depends(get_session),
    registry: ModelRegistryService = Depends(get_model_registry_service),
) -> MonitoringSummaryResponse:
    """
    Overall health, freshness, accuracy, drift counts, alerts, and a
    recommendation.

    The verdict is ``no_data`` until a forecast has been scored against an
    actual. It is not ``healthy``: a system that has measured nothing is not
    known to be working, and reporting it as healthy is the one mistake a
    monitoring page must not make.
    """
    span = window_days or monitoring_window_days()
    service = MonitoringService(session)
    production = _production_model(registry)
    version = model_version or (production or {}).get("version")

    payload = service.summary(
        model_version=version,
        window_days=span,
        production_model=production,
    )
    accuracy: WindowAccuracy = payload["accuracy"]
    can_detail = has_permission(user.role_enum, Permission.VIEW_MONITORING_DETAIL)

    return MonitoringSummaryResponse(
        status=payload["status"],
        has_data=payload["has_data"],
        window_days=span,
        window_start=payload["window_start"],
        window_end=payload["window_end"],
        model_version=version,
        freshness=_freshness_response(payload["freshness"]),
        accuracy=_accuracy_response(
            accuracy, window_days=span, model_version=version
        ),
        drift_summary=payload["drift"],
        alert_summary=payload["alerts"],
        retraining=_retraining_response(payload["retraining"]),
        no_data_message=payload["no_data_message"],
        can_manage_alerts=has_permission(user.role_enum, Permission.MANAGE_ALERTS),
        can_view_detail=can_detail,
    )


@router.get(
    "/accuracy",
    response_model=AccuracyResponse,
    summary="Forecast accuracy and its trend",
)
def accuracy(
    window_days: int = Query(default=0, ge=0, le=365),
    model_version: Optional[str] = Query(default=None),
    store_id: Optional[str] = Query(default=None),
    start: Optional[date] = Query(default=None),
    end: Optional[date] = Query(default=None),
    user: User = Depends(require_permission(Permission.VIEW_MONITORING)),
    session: Session = Depends(get_session),
    registry: ModelRegistryService = Depends(get_model_registry_service),
) -> AccuracyResponse:
    """
    Accuracy over the recent window, the window before it, and the stored
    history for the trend chart.

    The history comes from ``monitoring_metrics`` rather than being recomputed:
    the DAG's value for a date records what was true then, and recomputing it
    today would quietly rewrite the past as late actuals arrive.
    """
    span = window_days or monitoring_window_days()
    service = MonitoringService(session)
    version = model_version or (_production_model(registry) or {}).get("version")

    window = service.accuracy(
        window_days=span, model_version=version, store_id=store_id
    )
    series = service.accuracy_trend(
        model_version=version, store_id=store_id, start=start, end=end
    )
    return _accuracy_response(
        window,
        window_days=span,
        model_version=version,
        store_id=store_id,
        series=series,
    )


@router.get(
    "/data-freshness",
    response_model=FreshnessResponse,
    summary="How current the actuals and forecasts are",
)
def data_freshness(
    user: User = Depends(require_permission(Permission.VIEW_MONITORING)),
    session: Session = Depends(get_session),
) -> FreshnessResponse:
    """Latest actual, latest forecast, lag in days, and per-store coverage."""
    return _freshness_response(MonitoringService(session).freshness())


@router.get(
    "/drift",
    response_model=DriftResponse,
    summary="Per-feature drift",
    responses={403: {"description": "Requires detailed monitoring access."}},
)
def drift(
    model_version: Optional[str] = Query(default=None),
    user: User = Depends(require_permission(Permission.VIEW_MONITORING_DETAIL)),
    session: Session = Depends(get_session),
    registry: ModelRegistryService = Depends(get_model_registry_service),
) -> DriftResponse:
    """
    The latest drift verdict per feature.

    Requires ``VIEW_MONITORING_DETAIL``: a BUSINESS_USER sees that the model is
    healthy or not, while per-feature distribution scores are an analyst's and
    an engineer's concern.
    """
    service = MonitoringService(session)
    version = model_version or (_production_model(registry) or {}).get("version")
    rows = service.drift(model_version=version)

    return DriftResponse(
        has_data=bool(rows),
        model_version=version,
        features=[DriftRow(**row) for row in rows],
        drifting_features=sum(
            1 for row in rows if row["status"] == DriftStatus.DRIFT_DETECTED.value
        ),
        warning_features=sum(
            1 for row in rows if row["status"] == DriftStatus.WARNING.value
        ),
        no_data_message=None if rows else NO_DATA,
    )


@router.get("/alerts", response_model=AlertListResponse, summary="Monitoring alerts")
def alerts(
    status: Optional[str] = Query(default=None, description="open, acknowledged, resolved"),
    severity: Optional[str] = Query(default=None, description="info, warning, critical"),
    model_version: Optional[str] = Query(default=None),
    store_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_permission(Permission.VIEW_MONITORING)),
    session: Session = Depends(get_session),
) -> AlertListResponse:
    """
    Alerts, newest first. Defaults to the ones that still need attention.

    A resolved alert is kept but filtered out by default: the panel is a
    to-do list, and leaving closed items in it is how people stop reading it.
    """
    service = MonitoringService(session)
    statuses = (
        [status]
        if status
        else [AlertStatus.OPEN.value, AlertStatus.ACKNOWLEDGED.value]
    )
    rows, total = service.list_alerts(
        statuses=statuses,
        severities=[severity] if severity else None,
        model_version=model_version,
        store_id=store_id,
        limit=limit,
        offset=offset,
    )
    counts = service.monitoring.alert_counts_by_severity()
    return AlertListResponse(
        alerts=[AlertResponse.from_row(row) for row in rows],
        total=total,
        open_count=sum(counts.values()),
        critical_count=counts.get("critical", 0),
        warning_count=counts.get("warning", 0),
    )


@router.post(
    "/alerts/{alert_id}/acknowledge",
    response_model=AlertResponse,
    summary="Acknowledge an alert",
    responses={
        403: {"description": "Requires alert management permission."},
        404: {"description": "No such alert."},
    },
)
def acknowledge_alert(
    alert_id: str,
    request: Request,
    user: User = Depends(require_permission(Permission.MANAGE_ALERTS)),
    session: Session = Depends(get_session),
) -> AlertResponse:
    """
    Mark an alert as seen, without claiming the problem is fixed.

    Idempotent, and audited: who silenced an alert and when is exactly the
    question asked after an incident.
    """
    service = MonitoringService(session)
    alert = service.require_alert(alert_id)
    updated = service.monitoring.acknowledge_alert(alert, user_id=user.id)

    AuditService(session).record(
        action=AuditAction.ALERT_ACKNOWLEDGED,
        resource_type=ResourceType.ALERT,
        resource_id=str(updated.id),
        actor=actor_for(user, request),
        metadata={
            "alert_type": updated.alert_type,
            "severity": updated.severity,
            "model_version": updated.model_version,
        },
    )
    return AlertResponse.from_row(updated)


@router.post(
    "/alerts/{alert_id}/resolve",
    response_model=AlertResponse,
    summary="Resolve an alert",
    responses={
        403: {"description": "Requires alert management permission."},
        404: {"description": "No such alert."},
    },
)
def resolve_alert(
    alert_id: str,
    request: Request,
    payload: Optional[ResolveAlertRequest] = None,
    user: User = Depends(require_permission(Permission.MANAGE_ALERTS)),
    session: Session = Depends(get_session),
) -> AlertResponse:
    """
    Close an alert, optionally recording what was done.

    If the underlying condition recurs, the next evaluation opens a *new*
    alert rather than reopening this one: a problem returning after someone
    closed it is new information.
    """
    service = MonitoringService(session)
    alert = service.require_alert(alert_id)
    note = payload.note if payload else None
    updated = service.monitoring.resolve_alert(alert, user_id=user.id, note=note)

    AuditService(session).record(
        action=AuditAction.ALERT_RESOLVED,
        resource_type=ResourceType.ALERT,
        resource_id=str(updated.id),
        actor=actor_for(user, request),
        metadata={
            "alert_type": updated.alert_type,
            "severity": updated.severity,
            "note_supplied": bool(note),
        },
    )
    return AlertResponse.from_row(updated)


@router.post(
    "/evaluate",
    response_model=EvaluateResponse,
    summary="Run a monitoring evaluation",
    responses={
        403: {"description": "Requires alert management permission."},
        503: {"description": "Monitoring storage is unavailable."},
    },
)
def evaluate(
    payload: Optional[EvaluateRequest],
    request: Request,
    user: User = Depends(require_permission(Permission.MANAGE_ALERTS)),
    session: Session = Depends(get_session),
) -> EvaluateResponse:
    """
    Load recent actuals, score forecasts, measure drift, and raise alerts.

    Triggered on a schedule by the ``sales_forecast_monitoring`` DAG, and
    callable by hand to re-evaluate after a backfill.

    It lives here rather than in the DAG because Airflow 3 pins SQLAlchemy 1.4
    while these models use the 2.0 declarative API: a DAG task importing
    ``api.db`` would fail at import. Keeping the evaluation in one place beats
    maintaining a second, 1.4-compatible copy of the schema and the metrics.

    Reports zeros with a reason rather than failing when nothing is scorable --
    that is the normal state until forecasts reach their outcome dates.
    """
    from api.services.monitoring_evaluation import MonitoringEvaluator

    parameters = payload or EvaluateRequest()
    result = MonitoringEvaluator(session).run(parameters)

    AuditService(session).record(
        action=AuditAction.MONITORING_EVALUATED,
        resource_type=ResourceType.SYSTEM,
        resource_id=f"monitoring:{result.model_version}",
        actor=actor_for(user, request),
        metadata={
            "window_days": parameters.window_days,
            "scored_pairs": result.scored_pairs,
            "metrics_written": result.metrics_written,
            "alerts_created": result.alerts_created,
            "recommendation": result.recommendation,
            "status": result.status,
        },
    )

    logger.info(
        "Monitoring evaluation: status=%s scored=%d metrics=%d alerts=+%d/~%d "
        "recommendation=%s",
        result.status,
        result.scored_pairs,
        result.metrics_written,
        result.alerts_created,
        result.alerts_refreshed,
        result.recommendation,
    )
    return result


__all__ = ["TAG", "router"]
