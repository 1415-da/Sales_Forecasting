"""
Model registry: discovery, comparison, and verified mutations.

Reads require ``VIEW_MODEL_REGISTRY``, which every role holds; detail reads
additionally require ``VIEW_MODEL_REGISTRY_DETAIL``, so a BUSINESS_USER or
VIEWER sees what is serving but not its full lineage. Every mutation requires
its own capability, an explicit ``confirm``, and writes an audit record.

Route handlers stay thin: resolution and verification live in the services.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from api.core.errors import ScenarioValidationError
from api.core.rbac import Permission, has_permission
from api.db.models.audit import AuditAction, ResourceType
from api.db.models.user import User
from api.db.session import get_session
from api.dependencies.auth import actor_for, require_permission
from api.deps import get_forecast_service, get_registry_admin_service
from api.schemas.models import (
    ArtifactSummary,
    MetricDeltaRow,
    ModelArtifactsResponse,
    ModelComparisonResponse,
    ModelDetailResponse,
    ModelLineageResponse,
    ModelListResponse,
    RegistryActionRequest,
    RegistryActionResponse,
)
from api.services.audit_service import AuditService
from api.services.forecast_service import ForecastService
from api.services.registry_admin_service import (
    RegistryAction,
    RegistryAdminService,
)

logger = logging.getLogger(__name__)

TAG = "Models"

router = APIRouter(prefix="/v1/models", tags=[TAG])

#: Audit action per registry mutation. A rollback is mechanically a promotion,
#: but the audit trail must say which one was intended.
_AUDIT_ACTIONS = {
    RegistryAction.PROMOTE: AuditAction.MODEL_PROMOTED,
    RegistryAction.ARCHIVE: AuditAction.MODEL_ARCHIVED,
    RegistryAction.ROLLBACK: AuditAction.MODEL_ROLLED_BACK,
}

#: Artifacts every training run writes, described by role rather than by path.
_KNOWN_ARTIFACTS = (
    ("model", "estimator", "The fitted estimator used for inference."),
    ("feature_cols", "schema", "Ordered list of the model's input features."),
    ("scalers", "preprocessing", "Feature scaler fitted during training."),
    ("encoders", "preprocessing", "Categorical encoders fitted during training."),
)


def mlflow_browse_url(run_id: Optional[str]) -> Optional[str]:
    """
    A browsable MLflow run URL, when one is configured.

    From ``MLFLOW_PUBLIC_URL`` rather than ``MLFLOW_TRACKING_URI``: the
    tracking URI is an internal Docker hostname that a browser cannot resolve,
    so returning it would produce a link that never works.
    """
    base = os.getenv("MLFLOW_PUBLIC_URL", "").strip().rstrip("/")
    if not base or not run_id:
        return None
    return f"{base}/#/experiments/0?searchFilter=&selectedRunUuid={run_id}"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=ModelListResponse,
    summary="List the model version serving each configured model",
)
def list_models(
    user: User = Depends(require_permission(Permission.VIEW_MODEL_REGISTRY)),
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
    "/compare",
    response_model=ModelComparisonResponse,
    summary="Compare two model versions",
    responses={
        403: {"description": "Requires detailed registry access."},
        404: {"description": "One of the versions does not exist."},
        422: {"description": "Both versions are required, and must differ."},
    },
)
def compare_versions(
    version_a: str = Query(description="The incumbent version."),
    version_b: str = Query(description="The candidate version."),
    model: Optional[str] = Query(default=None),
    user: User = Depends(require_permission(Permission.VIEW_MODEL_REGISTRY_DETAIL)),
    service: ForecastService = Depends(get_forecast_service),
    admin: RegistryAdminService = Depends(get_registry_admin_service),
) -> ModelComparisonResponse:
    """
    Compare two versions metric by metric.

    Declared before ``/{model_version}`` so the literal path wins: registered
    after it, ``compare`` would be captured as a version number.

    ``version_a`` is the incumbent, ``version_b`` the candidate -- so
    "improved" means the candidate is better.
    """
    if version_a == version_b:
        raise ScenarioValidationError(
            "Choose two different versions to compare.", field="version_b"
        )

    chosen = (model or admin.settings.default_model).strip().lower()
    comparison = admin.compare(
        model=chosen, version_a=version_a, version_b=version_b
    )

    return ModelComparisonResponse(
        model=chosen,
        version_a=service._to_info(comparison.version_a, None),
        version_b=service._to_info(comparison.version_b, None),
        metrics=[
            MetricDeltaRow(
                metric=item.metric,
                version_a=item.version_a,
                version_b=item.version_b,
                delta=item.delta,
                delta_pct=item.delta_pct,
                verdict=item.verdict,
                lower_is_better=item.lower_is_better,
            )
            for item in comparison.metrics
        ],
        improved_count=comparison.improved_count,
        regressed_count=comparison.regressed_count,
        summary=comparison.summary,
    )


@router.get(
    "/{model_version}",
    response_model=ModelDetailResponse,
    summary="Detail for one model version",
    responses={404: {"description": "No such model version."}},
)
def get_model(
    model_version: str,
    model: str = Query(
        default="",
        description="Short model name. Defaults to the configured default model.",
    ),
    user: User = Depends(require_permission(Permission.VIEW_MODEL_REGISTRY)),
    service: ForecastService = Depends(get_forecast_service),
) -> ModelDetailResponse:
    """
    One version's metadata, feature list, and dataset lineage.

    ``model_version`` accepts a registry version number or ``latest`` for the
    newest Production version. Because each model is its own registered model
    in MLflow, the short model name is a query parameter: the path identifies
    the version, which is what the URL is nominally about.
    """
    return service.get_model(model_version, model or None)


@router.get(
    "/{model_version}/metrics",
    response_model=ModelDetailResponse,
    summary="Metrics for one model version",
    responses={404: {"description": "No such model version."}},
)
def get_model_metrics(
    model_version: str,
    model: str = Query(default=""),
    user: User = Depends(require_permission(Permission.VIEW_MODEL_PERFORMANCE)),
    service: ForecastService = Depends(get_forecast_service),
) -> ModelDetailResponse:
    """
    Training metrics for one version.

    Gated on ``VIEW_MODEL_PERFORMANCE`` rather than registry access, because
    "how good is the model" is a question every role may ask.
    """
    return service.get_model(model_version, model or None)


@router.get(
    "/{model_version}/lineage",
    response_model=ModelLineageResponse,
    summary="Where a model version came from",
    responses={
        403: {"description": "Requires detailed registry access."},
        404: {"description": "No such model version."},
    },
)
def get_lineage(
    model_version: str,
    model: Optional[str] = Query(default=None),
    user: User = Depends(require_permission(Permission.VIEW_MODEL_REGISTRY_DETAIL)),
    admin: RegistryAdminService = Depends(get_registry_admin_service),
) -> ModelLineageResponse:
    """
    The training run and dataset a version came from.

    No artifact paths. The API loads artifacts itself; handing a client a
    bucket location would leak infrastructure and invite bypassing the API.
    """
    chosen = (model or admin.settings.default_model).strip().lower()
    lineage = admin.lineage(chosen, model_version)
    return ModelLineageResponse(
        model=lineage["model"],
        version=lineage["version"],
        stage=lineage["stage"],
        run_id=lineage["run_id"],
        training_date=lineage["training_date"],
        dataset=lineage["dataset"],
        metrics=lineage["metrics"],
        mlflow_run_url=mlflow_browse_url(lineage["run_id"]),
    )


@router.get(
    "/{model_version}/artifacts",
    response_model=ModelArtifactsResponse,
    summary="Artifact metadata for one model version",
    responses={
        403: {"description": "Requires detailed registry access."},
        404: {"description": "No such model version."},
    },
)
def get_artifacts(
    model_version: str,
    model: Optional[str] = Query(default=None),
    user: User = Depends(require_permission(Permission.VIEW_MODEL_REGISTRY_DETAIL)),
    service: ForecastService = Depends(get_forecast_service),
    admin: RegistryAdminService = Depends(get_registry_admin_service),
) -> ModelArtifactsResponse:
    """
    Which artifacts a version has, described by role.

    Availability is determined by actually loading the bundle, so this answers
    "can this version serve a forecast" rather than "does a file exist".
    """
    chosen = (model or admin.settings.default_model).strip().lower()
    resolved = admin.version_detail(chosen, model_version)

    bundle = (
        service.artifacts.try_load_bundle(resolved.run_id, resolved.model)
        if resolved.run_id
        else None
    )
    warnings = []
    if bundle is None:
        warnings.append(
            "The artifacts for this version could not be loaded, so it cannot "
            "currently serve a forecast."
        )

    present = {
        "model": bundle is not None and bundle.model is not None,
        "feature_cols": bool(bundle and bundle.feature_cols),
        "scalers": bool(bundle and bundle.scaler is not None),
        "encoders": bool(bundle and bundle.encoders),
    }

    return ModelArtifactsResponse(
        model=resolved.model,
        version=resolved.version,
        run_id=resolved.run_id,
        artifacts=[
            ArtifactSummary(
                name=name,
                kind=kind,
                description=description,
                available=present.get(name, False),
            )
            for name, kind, description in _KNOWN_ARTIFACTS
        ],
        feature_count=bundle.feature_count if bundle else None,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def _mutate(
    *,
    action: RegistryAction,
    model_version: str,
    payload: RegistryActionRequest,
    request: Request,
    user: User,
    session: Session,
    admin: RegistryAdminService,
    service: ForecastService,
) -> RegistryActionResponse:
    """
    Shared body for every registry mutation.

    One implementation of confirm-check, apply, verify, audit, and re-read, so
    the three mutations cannot diverge on any of those steps.
    """
    if not payload.confirm:
        raise ScenarioValidationError(
            "This action changes which model serves every forecast. Resend with "
            '"confirm": true.',
            field="confirm",
        )

    chosen = (payload.model or admin.settings.default_model).strip().lower()

    if action is RegistryAction.PROMOTE:
        result = admin.promote(
            model=chosen,
            version=model_version,
            archive_existing=payload.archive_existing,
        )
    elif action is RegistryAction.ARCHIVE:
        result = admin.archive(model=chosen, version=model_version)
    else:
        result = admin.rollback(model=chosen, version=model_version)

    AuditService(session).record(
        action=_AUDIT_ACTIONS[action],
        resource_type=ResourceType.MODEL_VERSION,
        resource_id=f"{result.model}:{result.version}",
        actor=actor_for(user, request),
        outcome="success" if result.verified else "failure",
        metadata={
            "model": result.model,
            "version": result.version,
            "previous_stage": result.previous_stage,
            "new_stage": result.new_stage,
            "archived_versions": result.archived_versions,
            "verified": result.verified,
            "reason": payload.reason,
        },
    )

    # The cached bundle for the previously serving version would otherwise keep
    # answering forecasts after the pointer moved.
    service.artifacts.clear_cache()

    if result.verified:
        message = (
            f"Version {result.version} of '{result.model}' is now "
            f"{result.new_stage}."
        )
        if result.archived_versions:
            message += (
                " Archived: "
                + ", ".join(f"v{item}" for item in result.archived_versions)
                + "."
            )
    else:
        message = (
            f"The registry accepted the change, but version {result.version} is "
            f"not reporting as {result.new_stage}. Check the MLflow tracking "
            "server before relying on this."
        )

    return RegistryActionResponse(
        success=result.verified,
        action=result.action.value,
        model=result.model,
        version=result.version,
        previous_stage=result.previous_stage,
        new_stage=result.new_stage,
        archived_versions=result.archived_versions,
        verified=result.verified,
        message=message,
        models=service.list_models().models,
    )


_MUTATION_RESPONSES = {
    401: {"description": "Authentication is required."},
    403: {"description": "Your role cannot change the model registry."},
    404: {"description": "No such model version."},
    409: {"description": "That transition is not valid from the current stage."},
    422: {"description": "Confirmation is required."},
    502: {"description": "The registry rejected the operation."},
    503: {"description": "MLflow is unavailable."},
}


@router.post(
    "/{model_version}/promote",
    response_model=RegistryActionResponse,
    summary="Promote a version to Production",
    responses=_MUTATION_RESPONSES,
)
def promote_model(
    model_version: str,
    payload: RegistryActionRequest,
    request: Request,
    user: User = Depends(require_permission(Permission.PROMOTE_MODEL)),
    session: Session = Depends(get_session),
    admin: RegistryAdminService = Depends(get_registry_admin_service),
    service: ForecastService = Depends(get_forecast_service),
) -> RegistryActionResponse:
    """
    Make a version the one that serves forecasts.

    The previously serving version is archived by default, so exactly one
    version is ever in Production -- two would make "which model produced this
    forecast" unanswerable.

    The transition is verified by re-reading the registry. ``verified: false``
    means the call was accepted but did not take effect, which is reported
    rather than assumed away.
    """
    return _mutate(
        action=RegistryAction.PROMOTE,
        model_version=model_version,
        payload=payload,
        request=request,
        user=user,
        session=session,
        admin=admin,
        service=service,
    )


@router.post(
    "/{model_version}/archive",
    response_model=RegistryActionResponse,
    summary="Archive a version",
    responses=_MUTATION_RESPONSES,
)
def archive_model(
    model_version: str,
    payload: RegistryActionRequest,
    request: Request,
    user: User = Depends(require_permission(Permission.ARCHIVE_MODEL)),
    session: Session = Depends(get_session),
    admin: RegistryAdminService = Depends(get_registry_admin_service),
    service: ForecastService = Depends(get_forecast_service),
) -> RegistryActionResponse:
    """
    Retire a version.

    Refused for the version currently serving Production: archiving it would
    leave the API with nothing to load. Promote a replacement instead, which
    archives the old one as part of the same transition.
    """
    return _mutate(
        action=RegistryAction.ARCHIVE,
        model_version=model_version,
        payload=payload,
        request=request,
        user=user,
        session=session,
        admin=admin,
        service=service,
    )


@router.post(
    "/{model_version}/rollback",
    response_model=RegistryActionResponse,
    summary="Roll Production back to a previous version",
    responses=_MUTATION_RESPONSES,
)
def rollback_model(
    model_version: str,
    payload: RegistryActionRequest,
    request: Request,
    user: User = Depends(require_permission(Permission.ROLLBACK_MODEL)),
    session: Session = Depends(get_session),
    admin: RegistryAdminService = Depends(get_registry_admin_service),
    service: ForecastService = Depends(get_forecast_service),
) -> RegistryActionResponse:
    """
    Return Production to an earlier version.

    Mechanically a promotion, kept separate so the audit trail records the
    intent. Requires something to currently be in Production -- with nothing
    serving, this is a promotion, and calling it a rollback would misdescribe
    what happened.
    """
    return _mutate(
        action=RegistryAction.ROLLBACK,
        model_version=model_version,
        payload=payload,
        request=request,
        user=user,
        session=session,
        admin=admin,
        service=service,
    )


__all__ = ["TAG", "router"]
