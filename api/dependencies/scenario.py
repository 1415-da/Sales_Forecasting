"""
Dependency providers for the Scenario Planner routes.

Every service the routes need is produced here, behind a FastAPI dependency, so
the handlers stay free of construction logic and tests can swap any collaborator
through ``app.dependency_overrides``.

Services are cached per process: constructing them is cheap and lazy (no
connection or MLflow call happens until first use), but caching keeps the
checkpoint engine and its connection pool shared across requests.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

from api.core.errors import PredictionFailedError
from api.services.baseline_forecast_service import BaselineForecastService
from api.services.feature_schema_service import FeatureSchemaService
from api.services.forecast_checkpoint_service import ForecastCheckpointService
from api.services.scenario_audit_service import ScenarioAuditService
from api.services.scenario_capability_service import ScenarioCapabilityService
from api.services.scenario_feature_service import ScenarioFeatureService
from api.services.scenario_forecast_service import (
    CheckpointBaselineProvider,
    FeatureMatrixPredictor,
    FutureFeatureBuilder,
    ScenarioForecastService,
)
from api.services.scenario_impact_service import ScenarioImpactService
from api.services.scenario_rollback_service import ScenarioRollbackService
from api.services.scenario_validation import ScenarioValidationService

logger = logging.getLogger(__name__)

AUDIT_ROLE_ENV_VAR = "SCENARIO_AUDIT_REQUIRED_ROLE"

INFERENCE_NOT_CONFIGURED_MESSAGE = (
    "Scenario inference is not configured on this deployment: no future-feature "
    "builder or matrix predictor adapter is registered."
)


@lru_cache(maxsize=1)
def get_feature_schema_service() -> FeatureSchemaService:
    """Feature schema reader, backed by MLflow artifacts."""
    return FeatureSchemaService()


@lru_cache(maxsize=1)
def get_validation_service() -> ScenarioValidationService:
    """Scenario request validator."""
    return ScenarioValidationService()


@lru_cache(maxsize=1)
def get_capability_service() -> ScenarioCapabilityService:
    """Scenario capability detection for a model version."""
    return ScenarioCapabilityService(feature_schema_service=get_feature_schema_service())


@lru_cache(maxsize=1)
def get_checkpoint_service() -> ForecastCheckpointService:
    """Baseline forecast checkpoint store."""
    return ForecastCheckpointService()


@lru_cache(maxsize=1)
def get_audit_service() -> ScenarioAuditService:
    """Scenario audit event store."""
    return ScenarioAuditService()


@lru_cache(maxsize=1)
def get_rollback_service() -> ScenarioRollbackService:
    """Failure handling and rollback."""
    return ScenarioRollbackService(
        audit_service=get_audit_service(),
        checkpoint_service=get_checkpoint_service(),
    )


@lru_cache(maxsize=1)
def get_baseline_service() -> BaselineForecastService:
    """Baseline forecast generator."""
    return BaselineForecastService(
        feature_schema_service=get_feature_schema_service(),
        checkpoint_service=get_checkpoint_service(),
        validation_service=get_validation_service(),
    )


@lru_cache(maxsize=1)
def get_feature_service() -> ScenarioFeatureService:
    """Scenario feature override engine."""
    return ScenarioFeatureService()


@lru_cache(maxsize=1)
def get_impact_service() -> ScenarioImpactService:
    """Scenario impact calculator."""
    return ScenarioImpactService()


def get_future_feature_builder() -> FutureFeatureBuilder:
    """
    Builder for the baseline forecast-time feature matrix.

    No adapter exists yet: nothing in the codebase produces a forecast-time
    feature matrix (``SimplePredictor`` builds features row-by-row inside its own
    recursive loop). Rather than fabricate one, this raises with a message that
    names the gap, so ``POST /v1/scenario/compare`` fails loudly and
    diagnosably instead of returning a fabricated comparison.

    Override this dependency to supply the real adapter.
    """
    raise PredictionFailedError(
        INFERENCE_NOT_CONFIGURED_MESSAGE,
        details={"missing_adapter": "FutureFeatureBuilder"},
    )


def get_matrix_predictor() -> FeatureMatrixPredictor:
    """
    Predictor that runs a model over a prepared feature matrix.

    Not implemented yet, for the same reason as ``get_future_feature_builder``.
    Override this dependency to supply the real adapter.
    """
    raise PredictionFailedError(
        INFERENCE_NOT_CONFIGURED_MESSAGE,
        details={"missing_adapter": "FeatureMatrixPredictor"},
    )


def get_baseline_provider() -> CheckpointBaselineProvider:
    """Resolver for a supplied ``baseline_forecast_id``."""
    return CheckpointBaselineProvider(checkpoint_service=get_checkpoint_service())


def get_forecast_service() -> ScenarioForecastService:
    """
    Scenario comparison orchestrator.

    Not cached: it depends on the two inference adapters above, which a
    deployment (or a test) may override per request.
    """
    return ScenarioForecastService(
        feature_builder=get_future_feature_builder(),
        matrix_predictor=get_matrix_predictor(),
        baseline_service=get_baseline_service(),
        feature_service=get_feature_service(),
        impact_service=get_impact_service(),
        feature_schema_service=get_feature_schema_service(),
        baseline_provider=get_baseline_provider(),
    )


def require_privileged_role(x_role: Optional[str] = None) -> Optional[str]:
    """
    Authorisation gate for privileged endpoints.

    TODO(auth): this platform has no authentication layer yet -- no identity
    provider, no session, no token verification -- so there is no trustworthy
    identity to check a role against. Reading a role from a client-supplied
    header would be security theatre: any caller could set it.

    This dependency is therefore a deliberate no-op pass-through and the single
    place to wire real authorisation once authentication exists. Until then,
    ``GET /v1/scenario/{id}/audit`` must not be exposed to untrusted networks.

    Setting ``SCENARIO_AUDIT_REQUIRED_ROLE`` records the role that *should* be
    enforced; it is logged as a reminder but is not treated as a control.
    """
    required_role = os.getenv(AUDIT_ROLE_ENV_VAR)
    if required_role:
        logger.warning(
            "%s=%s is set but no authentication layer exists; the audit endpoint "
            "is NOT access-controlled.",
            AUDIT_ROLE_ENV_VAR,
            required_role,
        )
    return x_role


def reset_dependency_cache() -> None:
    """Clear cached services. Intended for tests and config reloads."""
    for provider in (
        get_feature_schema_service,
        get_validation_service,
        get_capability_service,
        get_checkpoint_service,
        get_audit_service,
        get_rollback_service,
        get_baseline_service,
        get_feature_service,
        get_impact_service,
    ):
        provider.cache_clear()
