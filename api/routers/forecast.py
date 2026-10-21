"""
The forecast endpoint.

Thin by design: request validation is in the schema, business logic is in
``ForecastService``, and every error is a taxonomy exception rendered by the
installed boundary. What remains here is the HTTP contract and the per-request
observability that the route is the only place able to record.
"""

from __future__ import annotations

import logging
import time
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from api.core.rbac import Permission
from api.db.models.audit import AuditAction, ResourceType
from api.db.models.user import User
from api.db.repositories.monitoring_repository import ForecastRepository
from api.db.session import get_session
from api.dependencies.auth import actor_for, require_permission
from api.deps import get_forecast_service
from api.schemas.forecast import ForecastRequest, ForecastResponse
from api.services.audit_service import AuditService
from api.services.forecast_service import ForecastService

logger = logging.getLogger(__name__)

TAG = "Forecast"

router = APIRouter(prefix="/v1", tags=[TAG])


@router.post(
    "/forecast",
    response_model=ForecastResponse,
    summary="Generate a multi-day sales forecast",
    responses={
        404: {"description": "The model or model version does not exist."},
        409: {
            "description": "The version is not in Production, or its feature "
            "contract cannot be satisfied."
        },
        401: {"description": "Authentication is required."},
        403: {"description": "Your role cannot generate forecasts."},
        422: {"description": "Invalid request, or insufficient history."},
        500: {"description": "The model failed to produce a forecast."},
        502: {"description": "A model artifact could not be retrieved."},
        503: {"description": "MLflow is unavailable."},
    },
)
def create_forecast(
    payload: ForecastRequest,
    request: Request,
    user: User = Depends(require_permission(Permission.GENERATE_FORECAST)),
    service: ForecastService = Depends(get_forecast_service),
    session: Session = Depends(get_session),
) -> ForecastResponse:
    """
    Forecast ``horizon_days`` days of sales for one store.

    The model is loaded from the MLflow registry inside this process: the
    caller never touches MLflow, MinIO, or an artifact path.

    Supply at least the configured minimum of history, ordered oldest-first.
    ``model_version`` may be a registry version number or ``latest`` for the
    newest Production version.

    Read ``warnings`` on the response. It is never decorative: it records which
    model inputs had to be reconstructed because they cannot be known for a
    future date, and how the uncertainty band was derived.
    """
    started = time.perf_counter()
    response = service.generate(payload)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # Persisted so monitoring can score it later: a forecast that was never
    # written down cannot be compared against the outcome, which makes every
    # accuracy figure unobtainable. Failure to record is logged and swallowed --
    # the user already has their forecast, and losing it to a storage problem
    # would be the worse outcome.
    _persist(session, response, user)

    AuditService(session).record(
        action=AuditAction.FORECAST_GENERATED,
        resource_type=ResourceType.FORECAST,
        resource_id=response.forecast_id,
        actor=actor_for(user, request),
        metadata={
            "store_id": response.store_id,
            "model": response.model,
            "model_version": response.model_version,
            "horizon_days": response.horizon_days,
            "history_days": response.metadata.history_days,
            "duration_ms": elapsed_ms,
        },
    )

    logger.info(
        "forecast generated request_id=%s forecast_id=%s store=%s model=%s "
        "version=%s stage=%s horizon=%s history_days=%s duration_ms=%s",
        getattr(getattr(request, "state", None), "request_id", None),
        response.forecast_id,
        response.store_id,
        response.model,
        response.model_version,
        response.model_stage,
        response.horizon_days,
        response.metadata.history_days,
        elapsed_ms,
    )
    return response


def _persist(session: Session, response: ForecastResponse, user: User) -> None:
    """
    Record the forecast and its daily points for later scoring.

    Best-effort by design. The forecast has already been computed and returned;
    making the response fail because the audit/monitoring write failed would
    trade a working feature for a bookkeeping one.
    """
    try:
        ForecastRepository(session).save_forecast(
            forecast_id=response.forecast_id,
            store_id=response.store_id,
            model_name=response.model,
            model_version=response.model_version,
            model_stage=response.model_stage,
            run_id=response.metadata.run_id,
            horizon_days=response.horizon_days,
            confidence_level=response.confidence_level,
            history_start_date=date.fromisoformat(
                response.metadata.history_start_date
            ),
            history_end_date=date.fromisoformat(response.metadata.history_end_date),
            history_days=response.metadata.history_days,
            metadata=response.metadata.model_dump(mode="json"),
            points=[
                {
                    "date": point.date,
                    "predicted_sales": point.predicted_sales,
                    "lower_bound": point.lower_bound,
                    "upper_bound": point.upper_bound,
                }
                for point in response.forecasts
            ],
            created_by_id=user.id,
            created_by_email=user.email,
        )
    except Exception as exc:  # noqa: BLE001 - never fail a produced forecast
        logger.error(
            "Could not persist forecast %s for monitoring: %s",
            response.forecast_id,
            exc,
        )


__all__ = ["TAG", "router"]
