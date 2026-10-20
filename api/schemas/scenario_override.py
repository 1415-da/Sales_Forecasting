"""
Scenario feature override contract.

Describes what the scenario feature engine changed, what it could not change,
and why. The override records are serialisable because they are reported back to
the caller; the result envelope is a dataclass because it carries a DataFrame
that stays in-process and is never put on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    import pandas as pd


class ScenarioOverrideErrorCode(str, Enum):
    """Machine-readable codes for scenario override failures."""

    SCENARIO_NOT_SUPPORTED = "SCENARIO_NOT_SUPPORTED"
    FEATURE_NOT_AVAILABLE = "FEATURE_NOT_AVAILABLE"
    INVALID_DATE_RANGE = "INVALID_DATE_RANGE"
    HISTORICAL_DATA_MODIFICATION_ATTEMPTED = "HISTORICAL_DATA_MODIFICATION_ATTEMPTED"
    TARGET_COLUMN_MODIFICATION_ATTEMPTED = "TARGET_COLUMN_MODIFICATION_ATTEMPTED"
    # Additive: lag/rolling columns are derived from the target, so overriding
    # them directly is a distinct failure from touching the target itself.
    PROTECTED_FEATURE_MODIFICATION_ATTEMPTED = "PROTECTED_FEATURE_MODIFICATION_ATTEMPTED"


class OverrideType(str, Enum):
    """How a feature value was changed."""

    SET_CONSTANT = "set_constant"  # assigned a fixed value
    MULTIPLY = "multiply"  # scaled relative to its baseline value
    RECOMPUTE = "recompute"  # recalculated from the scenario window
    CUSTOM = "custom"  # caller-supplied custom_overrides value


class ValueSummary(BaseModel):
    """Compact description of a column slice, before or after an override."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=0)
    dtype: str
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    distinct: Optional[List[Any]] = Field(
        default=None,
        description="Distinct values, only for low-cardinality columns.",
    )

    def describe(self) -> str:
        """Human-readable one-liner for logs and UI tooltips."""
        if self.distinct is not None:
            rendered = ", ".join(str(value) for value in self.distinct)
            return f"{self.count} rows [{rendered}]"
        if self.mean is not None:
            return (
                f"{self.count} rows mean={self.mean:.4g} "
                f"min={self.min:.4g} max={self.max:.4g}"
            )
        return f"{self.count} rows ({self.dtype})"


class AppliedOverride(BaseModel):
    """One feature the engine successfully changed."""

    model_config = ConfigDict(extra="forbid")

    feature_name: str
    override_type: OverrideType
    affected_date_count: int = Field(ge=0)
    original_value_summary: ValueSummary
    new_value_summary: ValueSummary
    affected_dates: List[date] = Field(default_factory=list)
    detail: Optional[str] = None


class UnsupportedOverride(BaseModel):
    """One feature the engine could not change, and why."""

    model_config = ConfigDict(extra="forbid")

    feature_name: str
    reason: str
    error_code: ScenarioOverrideErrorCode = (
        ScenarioOverrideErrorCode.FEATURE_NOT_AVAILABLE
    )


@dataclass
class ScenarioOverrideResult:
    """
    Output of the scenario feature engine.

    ``scenario_future_features`` is a copy: the caller's frame is never mutated.
    """

    scenario_future_features: "pd.DataFrame"
    applied_overrides: List[AppliedOverride] = field(default_factory=list)
    unsupported_overrides: List[UnsupportedOverride] = field(default_factory=list)
    modified_dates: List[date] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Whether anything was actually modified."""
        return bool(self.applied_overrides)

    @property
    def modified_feature_names(self) -> List[str]:
        """Names of every feature that was changed."""
        return [override.feature_name for override in self.applied_overrides]

    def to_summary(self) -> "ScenarioOverrideSummary":
        """Serialisable view, for inclusion in a comparison response."""
        return ScenarioOverrideSummary(
            applied_overrides=self.applied_overrides,
            unsupported_overrides=self.unsupported_overrides,
            modified_dates=self.modified_dates,
        )


class ScenarioOverrideSummary(BaseModel):
    """Wire-safe view of an override result, without the feature matrix."""

    model_config = ConfigDict(extra="forbid")

    applied_overrides: List[AppliedOverride] = Field(default_factory=list)
    unsupported_overrides: List[UnsupportedOverride] = Field(default_factory=list)
    modified_dates: List[date] = Field(default_factory=list)

    @property
    def modified_date_count(self) -> int:
        """Number of forecast days touched."""
        return len(self.modified_dates)


class ScenarioOverrideErrorResponse(BaseModel):
    """Structured error body for scenario override failures."""

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: str
    field: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
