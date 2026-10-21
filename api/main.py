"""
FastAPI application for the Sales Forecasting serving layer.

Composition only: build the app, install CORS and the error boundary, and
register routers. No business logic lives here.

Run locally with::

    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import Settings, get_settings
from api.middleware.error_handler import install_error_handlers
from api.observability.scenario_logging import configure_json_logging
from api.routers import forecast as forecast_router
from api.routers import health as health_router
from api.routers import models as models_router
from api.routers import scenario as scenario_router

logger = logging.getLogger(__name__)

API_DESCRIPTION = (
    "Serving layer for the Astro Sales Forecasting platform.\n\n"
    "All model loading and inference happens here: a client supplies a sales "
    "history and receives a forecast, and never touches MLflow, MinIO, "
    "PostgreSQL, or an artifact path.\n\n"
    "Endpoints are grouped as Forecast (generate a projection), Models "
    "(discover what is serving), Scenario Planner (predictive what-if "
    "analysis), and Operations (health and metrics)."
)

OPENAPI_TAGS = [
    {
        "name": forecast_router.TAG,
        "description": (
            "Multi-day sales forecasts from the Production model. Point "
            "forecasts carry a constructed uncertainty band; the method is "
            "reported on every response."
        ),
    },
    {
        "name": models_router.TAG,
        "description": (
            "Registered model versions, their stage, their training metrics, "
            "and the stores they were trained on."
        ),
    },
    {
        "name": scenario_router.TAG,
        "description": (
            "Predictive what-if analysis. Scenario results are estimates from "
            "the selected production model and do not establish causal impact."
        ),
    },
    {
        "name": health_router.TAG,
        "description": "Health, readiness, and metrics endpoints.",
    },
]


def install_cors(app: FastAPI, settings: Settings) -> None:
    """
    Allow the configured browser origins to call the API.

    An explicit origin list rather than ``*``: the API echoes a request id and
    will later carry authenticated calls, and a wildcard origin cannot be
    combined with credentials. Origins come from ``API_CORS_ALLOW_ORIGINS`` so a
    deployment on a different host does not need a code change.
    """
    origins = list(settings.cors_allow_origins)
    if not origins:
        logger.warning(
            "No CORS origins configured; browser clients will be blocked. "
            "Set API_CORS_ALLOW_ORIGINS."
        )
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
    )
    logger.info("CORS enabled for origins: %s", ", ".join(origins))


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    Build the application.

    A factory rather than a module-level singleton so tests can construct an
    isolated app (and its own dependency overrides) per case.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.api_title,
        version=settings.api_version,
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
    )

    # Events are emitted as JSON lines; configure that before any request can
    # be served.
    configure_json_logging()

    # Installs the request-id middleware and the exception handlers that render
    # every failure as the one documented error envelope.
    install_error_handlers(app)

    # Added last, so it is the outermost middleware. Starlette handles an
    # unhandled exception in ServerErrorMiddleware, which sits outside the user
    # middleware stack; unless CORS is outermost, that 500 would reach the
    # browser without CORS headers and surface as an opaque network error
    # instead of the error envelope the frontend knows how to render.
    install_cors(app, settings)

    app.include_router(health_router.router)
    app.include_router(models_router.router)
    app.include_router(forecast_router.router)
    app.include_router(scenario_router.router)

    logger.info(
        "Sales Forecasting API initialised (version %s)", settings.api_version
    )
    return app


app = create_app()
