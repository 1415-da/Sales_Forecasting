"""
Contracts for the model-discovery endpoints.

What a planner needs before running a forecast: which models exist, which
version of each is serving, how it scored, and what inputs it accepts. Fields
are optional wherever the source of truth may genuinely not have the value --
a registry entry with no backing run has no metrics, and saying so is better
than inventing a zero.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ModelMetrics(BaseModel):
    """
    Regression metrics recorded by the training run.

    All optional: training omits MAPE when the actuals contain zeros, and an
    older run may not have logged WAPE or bias at all.
    """

    model_config = ConfigDict(extra="forbid")

    rmse: Optional[float] = None
    mae: Optional[float] = None
    r2: Optional[float] = None
    mape: Optional[float] = None
    wape: Optional[float] = None
    bias: Optional[float] = None

    @property
    def is_empty(self) -> bool:
        """Whether no metric at all was available."""
        return all(
            getattr(self, name) is None for name in self.model_fields  # noqa: SLF001
        )


class ModelInfo(BaseModel):
    """One registered model version, as the serving layer sees it."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str = Field(description="Short name used in a forecast request.")
    label: str = Field(description="Display name.")
    registered_model_name: str
    version: str
    stage: str
    run_id: Optional[str] = None
    is_production: bool = False
    is_default: bool = False
    metrics: ModelMetrics = Field(default_factory=ModelMetrics)
    training_date: Optional[str] = Field(
        default=None, description="ISO-8601 creation time of the model version."
    )
    #: Store identifiers the model's encoder recognises. Empty when the
    #: encoder artifact could not be read, which the frontend treats as
    #: "store selection unavailable" rather than "no stores".
    supported_stores: List[str] = Field(default_factory=list)
    feature_count: Optional[int] = None
    data_source: Optional[str] = None
    dataset_version: Optional[str] = None


class ModelListResponse(BaseModel):
    """Everything the model selector needs in one request."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    success: bool = True
    models: List[ModelInfo] = Field(default_factory=list)
    default_model: Optional[str] = None
    production_available: bool = False
    #: Union of every listed model's supported stores, in registry order.
    supported_stores: List[str] = Field(default_factory=list)
    #: Why the list is empty or incomplete, when it is.
    warnings: List[str] = Field(default_factory=list)


class ModelDetailResponse(BaseModel):
    """One model version plus the detail a single-model view shows."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    success: bool = True
    model: ModelInfo
    #: Ordered feature names the model consumes, when the artifact is readable.
    features: List[str] = Field(default_factory=list)
    #: Dataset lineage params the training run logged, already sanitized.
    lineage: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


class MetricDeltaRow(BaseModel):
    """One metric compared between two versions."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    version_a: Optional[float] = None
    version_b: Optional[float] = None
    delta: Optional[float] = None
    delta_pct: Optional[float] = None
    #: "improved", "regressed", "unchanged", or "incomparable".
    verdict: str = "incomparable"
    lower_is_better: bool = True


class ModelComparisonResponse(BaseModel):
    """
    Two versions side by side.

    ``version_a`` is the incumbent and ``version_b`` the candidate, so
    "improved" always means the candidate is better. The order is part of the
    contract: swapping it inverts every verdict.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    success: bool = True
    model: str
    version_a: ModelInfo
    version_b: ModelInfo
    metrics: List[MetricDeltaRow] = Field(default_factory=list)
    improved_count: int = 0
    regressed_count: int = 0
    #: A sentence stating what the comparison shows, without overclaiming when
    #: no metric is recorded for both versions.
    summary: str = ""


class RegistryActionRequest(BaseModel):
    """
    Body for a registry mutation.

    ``confirm`` must be explicitly true. A promotion changes what every
    forecast in the platform is served by, so it should not be possible to
    trigger with an empty POST.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: Optional[str] = Field(
        default=None, description="Short model name. Defaults to the default model."
    )
    confirm: bool = Field(
        default=False, description="Must be true. Guards against an accidental call."
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Why this change is being made. Recorded in the audit log.",
    )
    archive_existing: bool = Field(
        default=True,
        description=(
            "Archive the version currently in Production, so exactly one "
            "version serves at a time."
        ),
    )


class RegistryActionResponse(BaseModel):
    """The result of a verified registry mutation."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    success: bool = True
    action: str
    model: str
    version: str
    previous_stage: str
    new_stage: str
    archived_versions: List[str] = Field(default_factory=list)
    #: Whether the new stage was confirmed by re-reading the registry. False
    #: means the call was accepted but the change did not take effect -- the
    #: exact failure that once left the registry with no Production version.
    verified: bool = False
    message: str = ""
    #: The registry as it stands after the change, so a client does not have to
    #: refetch to find out what happened.
    models: List[ModelInfo] = Field(default_factory=list)


class ModelLineageResponse(BaseModel):
    """
    Where a model version came from.

    No artifact paths: an object-store location is not something a browser
    should be handed, and it is not needed to understand provenance.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    success: bool = True
    model: str
    version: str
    stage: str
    run_id: Optional[str] = None
    training_date: Optional[str] = None
    dataset: Dict[str, Any] = Field(default_factory=dict)
    metrics: Dict[str, float] = Field(default_factory=dict)
    #: Resolved from configuration, for an operator to open the run. Omitted
    #: when no browsable tracking URL is configured.
    mlflow_run_url: Optional[str] = None


class ArtifactSummary(BaseModel):
    """One artifact, described without exposing where it lives."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    description: str
    available: bool = True


class ModelArtifactsResponse(BaseModel):
    """
    Artifact *metadata* for a version.

    Names and roles only. The API loads artifacts itself; handing a client a
    bucket path would both leak infrastructure and invite it to bypass the API.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    success: bool = True
    model: str
    version: str
    run_id: Optional[str] = None
    artifacts: List[ArtifactSummary] = Field(default_factory=list)
    feature_count: Optional[int] = None
    warnings: List[str] = Field(default_factory=list)


__all__ = [
    "ArtifactSummary",
    "MetricDeltaRow",
    "ModelArtifactsResponse",
    "ModelComparisonResponse",
    "ModelDetailResponse",
    "ModelInfo",
    "ModelLineageResponse",
    "ModelListResponse",
    "ModelMetrics",
    "RegistryActionRequest",
    "RegistryActionResponse",
]
