"""
FastAPI dependency providers.

One module owns the lifetime of each service so routers never construct their
own. That is what makes ``app.dependency_overrides`` enough to test a route in
full isolation, and it keeps the expensive objects -- the MLflow client, the
in-memory model cache -- shared across requests instead of rebuilt per call.

Services are created on first use and then reused for the process's lifetime.
They hold no per-request state: a cached model bundle and a registry client are
both safe to share, and the model cache is explicitly lock-guarded.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from api.config import Settings, get_settings
from api.services.artifact_service import ArtifactService
from api.services.forecast_service import ForecastService
from api.services.model_registry_service import ModelRegistryService
from api.services.registry_admin_service import RegistryAdminService

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_artifact_service() -> ArtifactService:
    """Process-wide artifact loader, so the model cache is shared."""
    return ArtifactService(get_settings())


@lru_cache(maxsize=1)
def get_model_registry_service() -> ModelRegistryService:
    """Process-wide registry client."""
    return ModelRegistryService(get_settings())


@lru_cache(maxsize=1)
def get_forecast_service() -> ForecastService:
    """Process-wide forecast service, wired to the shared collaborators."""
    return ForecastService(
        settings=get_settings(),
        registry=get_model_registry_service(),
        artifacts=get_artifact_service(),
    )


@lru_cache(maxsize=1)
def get_registry_admin_service() -> RegistryAdminService:
    """
    Process-wide registry mutation service.

    Shares the read-side registry client so a mutation and the verification
    that follows it go through the same configuration.
    """
    return RegistryAdminService(
        settings=get_settings(), registry=get_model_registry_service()
    )


def get_app_settings() -> Settings:
    """Resolved settings, as a dependency."""
    return get_settings()


def reset_services() -> None:
    """
    Drop every cached service.

    For tests and for a configuration reload: without this, a patched
    environment would be ignored by services already built from the old one.
    """
    get_artifact_service.cache_clear()
    get_model_registry_service.cache_clear()
    get_forecast_service.cache_clear()
    get_registry_admin_service.cache_clear()
    get_settings.cache_clear()


__all__ = [
    "get_app_settings",
    "get_artifact_service",
    "get_forecast_service",
    "get_model_registry_service",
    "get_registry_admin_service",
    "reset_services",
]
