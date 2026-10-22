"""
Scenario comparison contract.

Describes a baseline-vs-scenario what-if comparison: the two forecast series,
the computed impact (point and quantile), what the override engine changed, and
the standing limitations that must accompany every response.

Name note: ``api.schemas.scenario`` carries an earlier, narrower
``ScenarioComparisonResponse`` from the first scenario-planner task. The model
here is the authoritative comparison contract; the older one is left untouched
so existing tests keep passing, and consolidating the two is a follow-up.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.baseline_forecast import UncertaintyMethod
from api.schemas.scenario import SCENARIO_SCHEMA_VERSION, ScenarioAssumptions, ScenarioType
from api.schemas.scenario_override import AppliedOverride, UnsupportedOverride

SCENARIO_ID_PREFIX = "scn_"
SCENARIO_FORECAST_ID_PREFIX = "sf_"

QUANTILE_P10 = 0.1
QUANTILE_P50 = 0.5
QUANTILE_P90 = 0.9
REPORTED_QUANTILES: Tuple[float, ...] = (QUANTILE_P10, QUANTILE_P50, QUANTILE_P90)

# Attached to every comparison response, verbatim and unconditionally.
SCENARIO_LIMITATIONS: Tuple[str, ...] = (
    "This is a predictive what-if estimate based on the selected production model.",
    "It does not establish causal impact.",
    "Assumes scenario effects resemble historical patterns.",
    "Uncertainty reflects model error and does not include external shocks.",
)


class ForecastType(str, Enum):
    """The only kind of forecast this service produces."""

    PREDICTIVE_WHAT_IF = "predictive_what_if"


def new_scenario_id() -> str:
    """Opaque identifier for one comparison."""
    return f"{SCENARIO_ID_PREFIX}{uuid.uuid4().hex}"


def new_scenario_forecast_id() -> str:
    """Opaque identifier for the scenario-side forecast."""
    return f"{SCENARIO_FORECAST_ID_PREFIX}{uuid.uuid4().hex}"


class ForecastSeriesSummary(BaseModel):
    """Aggregate view of one side of the comparison."""

    model_config = ConfigDict(extra="forbid")

    forecast_id: Optional[str] = None
    horizon_days: int = Field(ge=0)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    total_forecast: float = 0.0
    average_daily_forecast: float = 0.0
    min_daily_forecast: float = 0.0
    max_daily_forecast: float = 0.0
    uncertainty_method: Optional[UncertaintyMethod] = None
    has_quantiles: bool = False


class DailyImpact(BaseModel):
    """Baseline vs scenario for a single forecast day."""

    model_config = ConfigDict(extra="forbid")

    date: date
    baseline_forecast: float
    scenario_forecast: float
    daily_difference: float
    daily_difference_pct: Optional[float] = Field(
        default=None, description="None when the baseline value is zero or negative."
    )
    cumulative_difference: float = 0.0

    baseline_p10: Optional[float] = None
    baseline_p50: Optional[float] = None
    baseline_p90: Optional[float] = None
    scenario_p10: Optional[float] = None
    scenario_p50: Optional[float] = None
    scenario_p90: Optional[float] = None
    difference_p10: Optional[float] = None
    difference_p50: Optional[float] = None
    difference_p90: Optional[float] = None


class QuantileImpact(BaseModel):
    """Impact computed at one quantile level."""

    model_config = ConfigDict(extra="forbid")

    quantile: float = Field(gt=0.0, lt=1.0)
    total_baseline_forecast: float
    total_scenario_forecast: float
    total_difference: float
    total_difference_pct: Optional[float] = None


class ImpactRange(BaseModel):
    """Total impact expressed as a range rather than a single number."""

    model_config = ConfigDict(extra="forbid")

    total_difference_p10: Optional[float] = None
    total_difference_p50: Optional[float] = None
    total_difference_p90: Optional[float] = None

    @property
    def is_available(self) -> bool:
        """Whether any quantile impact could be computed."""
        return any(
            value is not None
            for value in (
                self.total_difference_p10,
                self.total_difference_p50,
                self.total_difference_p90,
            )
        )

    @property
    def spread(self) -> Optional[float]:
        """Width between the p10 and p90 total impacts."""
        if self.total_difference_p10 is None or self.total_difference_p90 is None:
            return None
        return self.total_difference_p90 - self.total_difference_p10


class ScenarioImpact(BaseModel):
    """Difference between the scenario and the baseline across the horizon."""

    model_config = ConfigDict(extra="forbid")

    horizon_days: int = Field(ge=0)
    dates: List[date] = Field(default_factory=list)
    daily_difference: List[float] = Field(default_factory=list)
    daily_difference_pct: List[Optional[float]] = Field(default_factory=list)
    cumulative_difference: List[float] = Field(default_factory=list)

    total_baseline_forecast: float = 0.0
    total_scenario_forecast: float = 0.0
    total_difference: float = 0.0
    total_difference_pct: Optional[float] = Field(
        default=None,
        description="None when the baseline total is zero or negative.",
    )

    positive_days: int = Field(default=0, ge=0)
    negative_days: int = Field(default=0, ge=0)
    neutral_days: int = Field(default=0, ge=0)

    peak_impact_date: Optional[date] = None
    peak_impact_value: Optional[float] = None

    quantile_impacts: List[QuantileImpact] = Field(default_factory=list)
    impact_range: ImpactRange = Field(default_factory=ImpactRange)
    daily_impacts: List[DailyImpact] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)

    @property
    def direction(self) -> str:
        """Overall direction of the impact."""
        if self.total_difference > 0:
            return "increase"
        if self.total_difference < 0:
            return "decrease"
        return "neutral"


class ScenarioComparisonResponse(BaseModel):
    """A complete baseline-vs-scenario what-if comparison."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    scenario_id: str
    baseline_forecast_id: str
    scenario_forecast_id: str
    #: Model family the comparison ran on (``ensemble``, ``xgboost``, ...).
    model: Optional[str] = None
    model_version: str
    model_stage: str
    scenario_type: ScenarioType
    forecast_type: ForecastType = ForecastType.PREDICTIVE_WHAT_IF

    baseline: ForecastSeriesSummary
    scenario: ForecastSeriesSummary
    impact: ScenarioImpact
    daily_forecasts: List[DailyImpact] = Field(default_factory=list)

    applied_overrides: List[AppliedOverride] = Field(default_factory=list)
    unsupported_overrides: List[UnsupportedOverride] = Field(default_factory=list)
    modified_features: List[str] = Field(default_factory=list)
    modified_dates: List[date] = Field(default_factory=list)

    limitations: List[str] = Field(default_factory=lambda: list(SCENARIO_LIMITATIONS))
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    store_id: Optional[str] = None
    assumptions: Optional[ScenarioAssumptions] = None
    baseline_reused: bool = False
    warnings: List[str] = Field(default_factory=list)
    schema_version: str = SCENARIO_SCHEMA_VERSION

    # Additive, nullable: dataset lineage for the model behind this comparison,
    # populated by DatasetLineageService.attach_lineage. Typed as a mapping
    # rather than the lineage model itself to keep schemas free of a dependency
    # on services; moving ScenarioDatasetLineage into api/schemas/ would let
    # this be strongly typed.
    dataset_lineage: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Lineage of the training dataset behind this model version, "
            "including lineage_status when it could not be fully resolved."
        ),
    )


class ScenarioComparisonErrorResponse(BaseModel):
    """Structured error body for comparison failures."""

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: str
    field: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
