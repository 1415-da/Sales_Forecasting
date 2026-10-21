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


__all__ = [
    "ModelDetailResponse",
    "ModelInfo",
    "ModelListResponse",
    "ModelMetrics",
]
