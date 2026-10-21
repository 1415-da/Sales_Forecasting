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

from fastapi import APIRouter, Depends, Request

from api.deps import get_forecast_service
from api.schemas.forecast import ForecastRequest, ForecastResponse
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
        422: {"description": "Invalid request, or insufficient history."},
        500: {"description": "The model failed to produce a forecast."},
        502: {"description": "A model artifact could not be retrieved."},
        503: {"description": "MLflow is unavailable."},
    },
)
def create_forecast(
    payload: ForecastRequest,
    request: Request,
    service: ForecastService = Depends(get_forecast_service),
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


__all__ = ["TAG", "router"]
