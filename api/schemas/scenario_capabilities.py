"""
Model-aware scenario capability contract.

``api.schemas.scenario.ScenarioCapability`` describes what a scenario type means
*statically* (its levers and limits). The models here answer a different
question: given the feature schema of one trained model version, which scenario
types can that model actually support?

Note the deliberate overlap: ``ScenarioCapabilitiesResponse`` here supersedes the
static same-named model in ``api.schemas.scenario`` for the capabilities
endpoint, because the frontend needs per-model answers plus the request limits in
a single call. The static model is left untouched so existing callers keep
working; consolidating the two is a follow-up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.schemas.scenario import (
    DEFAULT_CONFIDENCE_LEVELS,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_MODEL_VERSION,
    MAX_HORIZON_DAYS,
    MIN_HISTORY_ROWS,
    MIN_HORIZON_DAYS,
    SCENARIO_SCHEMA_VERSION,
    ScenarioType,
)

FEATURE_SCHEMA_VERSION = "v1"


class RequirementMode(str, Enum):
    """How a feature requirement is satisfied."""

    ANY = "any"  # at least one candidate feature must be present
    ALL = "all"  # every candidate feature must be present


class FeatureSchema(BaseModel):
    """
    The feature contract of one trained model version.

    Mirrors the ``feature_schema.json`` artifact written at training time.
    Unknown keys are ignored so the training side can add metadata later
    without breaking older readers.
    """

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    schema_version: str = FEATURE_SCHEMA_VERSION
    model_version: str
    features: List[str] = Field(min_length=1)
    run_id: Optional[str] = None
    source_uri: Optional[str] = None

    @field_validator("features")
    @classmethod
    def _strip_and_drop_blanks(cls, value: List[str]) -> List[str]:
        """Normalise whitespace and reject a schema with no usable feature names."""
        cleaned = [name.strip() for name in value if name and name.strip()]
        if not cleaned:
            raise ValueError("features must contain at least one non-empty name")
        return cleaned

    @property
    def feature_set(self) -> frozenset[str]:
        """Case-insensitive lookup set of feature names."""
        return frozenset(name.lower() for name in self.features)

    def has_feature(self, name: str) -> bool:
        """Whether ``name`` exists in the schema, ignoring case and surrounding space."""
        return name.strip().lower() in self.feature_set

    def present(self, names: List[str]) -> List[str]:
        """Subset of ``names`` present in the schema, in the order given."""
        return [name for name in names if self.has_feature(name)]

    def absent(self, names: List[str]) -> List[str]:
        """Subset of ``names`` missing from the schema, in the order given."""
        return [name for name in names if not self.has_feature(name)]


class ScenarioCapabilityStatus(BaseModel):
    """Whether one scenario type is supported by a specific model version."""

    model_config = ConfigDict(extra="forbid")

    scenario_type: ScenarioType
    supported: bool
    required_features: List[str] = Field(
        default_factory=list,
        description="Candidate features that can satisfy this scenario type.",
    )
    available_features: List[str] = Field(
        default_factory=list,
        description="Candidates actually present in the model feature schema.",
    )
    missing_features: List[str] = Field(
        default_factory=list,
        description=(
            "Features blocking support; empty when supported. For an any-of "
            "requirement this lists every candidate when none was found."
        ),
    )
    requirement_mode: RequirementMode = RequirementMode.ANY
    reason: Optional[str] = Field(
        default=None, description="Why the scenario is unsupported; None when supported."
    )


class ScenarioCapabilitiesResponse(BaseModel):
    """Per-model scenario capabilities plus the request limits, in one payload."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_version: str
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    run_id: Optional[str] = None
    feature_count: int = Field(default=0, ge=0)
    features: List[str] = Field(default_factory=list)
    capabilities: List[ScenarioCapabilityStatus] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    schema_version: str = SCENARIO_SCHEMA_VERSION
    min_history_rows: int = MIN_HISTORY_ROWS
    min_horizon_days: int = MIN_HORIZON_DAYS
    max_horizon_days: int = MAX_HORIZON_DAYS
    default_horizon_days: int = DEFAULT_HORIZON_DAYS
    default_model_version: str = DEFAULT_MODEL_VERSION
    default_confidence_levels: List[float] = Field(
        default_factory=lambda: list(DEFAULT_CONFIDENCE_LEVELS)
    )
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def supported_scenario_types(self) -> List[ScenarioType]:
        """Scenario types this model version can serve."""
        return [cap.scenario_type for cap in self.capabilities if cap.supported]

    def capability_for(self, scenario_type: ScenarioType) -> Optional[ScenarioCapabilityStatus]:
        """Status for one scenario type, or None when not evaluated."""
        for capability in self.capabilities:
            if capability.scenario_type is scenario_type:
                return capability
        return None

    def is_supported(self, scenario_type: ScenarioType) -> bool:
        """Convenience check used by the scenario engine before it runs."""
        capability = self.capability_for(scenario_type)
        return bool(capability and capability.supported)


class FeatureSchemaErrorResponse(BaseModel):
    """Structured error body for feature-schema resolution failures."""

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: str
    field: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
