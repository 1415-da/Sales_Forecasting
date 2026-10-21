"""
Model discovery endpoints.

Thin: resolve a dependency, call the service, return its typed response. Every
failure mode is raised by the service as a taxonomy error and rendered by the
installed error boundary, so there is no try/except here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from api.deps import get_forecast_service
from api.schemas.models import ModelDetailResponse, ModelListResponse
from api.services.forecast_service import ForecastService

logger = logging.getLogger(__name__)

TAG = "Models"

router = APIRouter(prefix="/v1/models", tags=[TAG])


@router.get(
    "",
    response_model=ModelListResponse,
    summary="List the model version serving each configured model",
)
def list_models(
    service: ForecastService = Depends(get_forecast_service),
) -> ModelListResponse:
    """
    Every model a forecast request may name, with its serving version.

    Returns 200 with an empty list and an explanatory warning when the registry
    holds nothing yet: "no models have been trained" is a state the UI must
    render, not an error it should retry.
    """
    return service.list_models()


@router.get(
    "/{model_version}",
    response_model=ModelDetailResponse,
    summary="Detail for one model version",
)
def get_model(
    model_version: str,
    model: str = Query(
        default="",
        description="Short model name. Defaults to the configured default model.",
    ),
    service: ForecastService = Depends(get_forecast_service),
) -> ModelDetailResponse:
    """
    One version's metadata, feature list, and dataset lineage.

    ``model_version`` accepts a registry version number or ``latest`` for the
    newest Production version. Because each model is its own registered model
    in MLflow, the short model name is a query parameter rather than part of
    the path -- the path identifies the version, which is what the URL is
    nominally about.
    """
    return service.get_model(model_version, model or None)


__all__ = ["TAG", "router"]
