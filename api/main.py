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

from fastapi import FastAPI

from api.middleware.error_handler import install_error_handlers
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
    {"name": "Operations", "description": "Health and readiness probes."},
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

    # Installs the request-id middleware and the exception handlers that render
    # every failure as the one documented error envelope.
    install_error_handlers(app)

    app.include_router(scenario_router.router)

    @app.get("/health", tags=["Operations"], summary="Liveness probe")
    def health() -> Dict[str, Any]:
        """Report that the process is up. Does not touch MLflow or the database."""
        return {"success": True, "status": "ok", "version": API_VERSION}

    logger.info("Sales Forecasting API initialised (version %s)", API_VERSION)
    return app


app = create_app()
