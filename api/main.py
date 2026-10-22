"""
FastAPI application for the Sales Forecasting serving layer.

Composition only: build the app, install CORS and the error boundary, and
register routers. No business logic lives here.

Run locally with::

    uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.bootstrap import run_startup_tasks
from api.config import Settings, get_settings
from api.middleware.error_handler import install_error_handlers
from api.observability.scenario_logging import configure_json_logging
from api.routers import admin_audit as admin_audit_router
from api.routers import admin_users as admin_users_router
from api.routers import auth as auth_router
from api.routers import forecast as forecast_router
from api.routers import health as health_router
from api.routers import models as models_router
from api.routers import monitoring as monitoring_router
from api.routers import scenario as scenario_router
from api.routers import training as training_router

logger = logging.getLogger(__name__)

API_DESCRIPTION = (
    "Serving layer for the Astro Sales Forecasting platform.\n\n"
    "All model loading and inference happens here: a client supplies a sales "
    "history and receives a forecast, and never touches MLflow, MinIO, "
    "PostgreSQL, or an artifact path.\n\n"
    "**Authentication.** Every `/v1/*` route requires a bearer access token "
    "from `POST /auth/login`. The refresh token is delivered as an HttpOnly "
    "cookie and exchanged at `POST /auth/refresh`. `/auth/*` and `/health*` "
    "are public.\n\n"
    "**Authorization.** Each route requires a named capability. A missing "
    "capability is a 403 and is recorded in the audit log; the role/permission "
    "matrix is served at `GET /v1/admin/permissions`."
)

OPENAPI_TAGS = [
    {
        "name": auth_router.TAG,
        "description": (
            "Sign-in, sign-out, token refresh, and the current user. The only "
            "routes that do not require an access token."
        ),
    },
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
        "name": monitoring_router.TAG,
        "description": (
            "Forecast accuracy, data freshness, drift, and alerts. Populated "
            "by the monitoring DAG; endpoints report an explicit no-data state "
            "rather than inventing figures."
        ),
    },
    {
        "name": admin_users_router.TAG,
        "description": "User and role administration. ADMIN only.",
    },
    {
        "name": admin_audit_router.TAG,
        "description": (
            "Audit trail of privileged and attributable actions. ADMIN sees "
            "everything; ML_ENGINEER sees model and monitoring activity."
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
        # PATCH is used by the admin user editor; Authorization carries the
        # bearer token on every /v1 call. Omitting either fails the browser's
        # preflight before the request is ever sent.
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "Idempotency-Key",
        ],
        expose_headers=["X-Request-ID", "Content-Disposition"],
    )
    logger.info("CORS enabled for origins: %s", ", ".join(origins))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Run start-up tasks before the first request is served.

    A lifespan handler rather than the deprecated ``on_event``. The work --
    ensuring the schema exists and creating the first admin when there is none
    -- is deliberately tolerant of failure: a database that is not ready yet
    should leave the API serving ``/health`` (which reports it as unavailable)
    rather than crash-looping the container.
    """
    run_startup_tasks()
    yield


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
        lifespan=lifespan,
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
    app.include_router(auth_router.router)
    app.include_router(models_router.router)
    app.include_router(forecast_router.router)
    app.include_router(scenario_router.router)
    app.include_router(monitoring_router.router)
    app.include_router(training_router.router)
    app.include_router(admin_users_router.router)
    app.include_router(admin_audit_router.router)



    logger.info(
        "Sales Forecasting API initialised (version %s)", settings.api_version
    )
    return app


app = create_app()
