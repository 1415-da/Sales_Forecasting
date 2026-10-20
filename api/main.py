"""
FastAPI application for the Sales Forecasting serving layer.

Composition only: build the app, install the error boundary and request-id
middleware, and register routers. No business logic lives here.

Run locally with::

    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from fastapi import FastAPI, Response

from api.middleware.error_handler import install_error_handlers
from api.observability.health import build_health_report
from api.observability.scenario_logging import configure_json_logging
from api.observability.scenario_metrics import CONTENT_TYPE_PROMETHEUS, get_metrics
from api.routers import scenario as scenario_router

logger = logging.getLogger(__name__)

API_TITLE = "Sales Forecasting API"
API_VERSION = "0.1.0"
API_DESCRIPTION = (
    "Serving layer for the Astro Sales Forecasting platform. Exposes the "
    "Scenario Planner: capability discovery, baseline forecasts, what-if "
    "comparisons, rollback, and audit history."
)

OPENAPI_TAGS = [
    {
        "name": scenario_router.TAG,
        "description": (
            "Predictive what-if analysis. Scenario results are estimates from the "
            "selected production model and do not establish causal impact."
        ),
    },
    {
        "name": "Operations",
        "description": "Health, readiness, and metrics endpoints.",
    },
]


def create_app() -> FastAPI:
    """
    Build the application.

    A factory rather than a module-level singleton so tests can construct an
    isolated app (and its own dependency overrides) per case.
    """
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
    )

    # Scenario events are emitted as JSON lines; configure that before any
    # request can be served.
    configure_json_logging()

    # Installs the request-id middleware and the exception handlers that render
    # every failure as the one documented error envelope.
    install_error_handlers(app)

    app.include_router(scenario_router.router)

    @app.get("/health", tags=["Operations"], summary="Health and dependencies")
    def health(deep: bool = True) -> Dict[str, Any]:
        """
        Report API, database, MLflow, MinIO, and feature-schema state.

        Always 200 while the process itself is serving: an orchestrator should
        not restart a healthy API because an upstream is briefly down. Use
        ``/health/ready`` for a probe that fails on a critical dependency, or
        ``?deep=false`` for a cheap check that touches nothing.
        """
        return build_health_report(API_VERSION, deep=deep).to_dict()

    @app.get("/health/ready", tags=["Operations"], summary="Readiness probe")
    def readiness(response: Response) -> Dict[str, Any]:
        """503 when a dependency the planner cannot work without is unavailable."""
        report = build_health_report(API_VERSION, deep=True)
        if not report.is_ready:
            response.status_code = 503
        payload = report.to_dict()
        payload["ready"] = report.is_ready
        return payload

    @app.get("/metrics", tags=["Operations"], summary="Prometheus metrics")
    def metrics() -> Response:
        """Scenario metrics in the Prometheus text exposition format."""
        return Response(
            content=get_metrics().render(), media_type=CONTENT_TYPE_PROMETHEUS
        )

    logger.info("Sales Forecasting API initialised (version %s)", API_VERSION)
    return app


app = create_app()
