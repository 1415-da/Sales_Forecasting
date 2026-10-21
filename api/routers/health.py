"""
Health, readiness, and metrics endpoints.

Moved out of ``main.py`` so composition stays composition. The probe logic
itself lives in ``api.observability.health``; this module only decides status
codes and response shape.

``/health`` gains the current Production model over what the probes alone
report, because "is the API up" and "can it actually serve a forecast" are
different questions and an operator needs both in one place.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Response

from api.config import Settings
from api.deps import get_app_settings, get_model_registry_service
from api.observability.health import build_health_report
from api.observability.scenario_metrics import CONTENT_TYPE_PROMETHEUS, get_metrics
from api.services.model_registry_service import ModelRegistryService

logger = logging.getLogger(__name__)

TAG = "Operations"

router = APIRouter(tags=[TAG])


@router.get("/health", summary="Health, dependencies, and the serving model")
def health(
    deep: bool = True,
    settings: Settings = Depends(get_app_settings),
    registry: ModelRegistryService = Depends(get_model_registry_service),
) -> Dict[str, Any]:
    """
    Report API, database, MLflow, MinIO, feature-schema, and model state.

    Always 200 while the process itself is serving: an orchestrator should not
    restart a healthy API because an upstream is briefly down. Use
    ``/health/ready`` for a probe that fails on a critical dependency, or
    ``?deep=false`` for a cheap check that touches nothing.
    """
    payload = build_health_report(settings.api_version, deep=deep).to_dict()
    payload["production_model"] = (
        registry.production_summary() if deep else None
    )
    return payload


@router.get("/health/ready", summary="Readiness probe")
def readiness(
    response: Response,
    settings: Settings = Depends(get_app_settings),
) -> Dict[str, Any]:
    """503 when a dependency the API cannot work without is unavailable."""
    report = build_health_report(settings.api_version, deep=True)
    if not report.is_ready:
        response.status_code = 503
    payload = report.to_dict()
    payload["ready"] = report.is_ready
    return payload


@router.get("/metrics", summary="Prometheus metrics")
def metrics() -> Response:
    """Serving metrics in the Prometheus text exposition format."""
    return Response(content=get_metrics().render(), media_type=CONTENT_TYPE_PROMETHEUS)


__all__ = ["TAG", "router"]
