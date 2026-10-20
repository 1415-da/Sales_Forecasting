"""
Scenario planning (what-if) contract.

Domain layer only: these models describe the shape of a scenario request and
its comparison response. Nothing here performs model inference, persistence, or
routing, and nothing here imports from the Airflow DAG or the training package.

Numeric limits are declared as module constants because they are part of the
public contract; ``api.services.scenario_config`` imports them rather than
redefining them, so a limit has exactly one source of truth.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCENARIO_SCHEMA_VERSION = "v1"

# Request limits (part of the public contract).
MIN_HISTORY_ROWS = 7
MIN_HORIZON_DAYS = 1
MAX_HORIZON_DAYS = 90
DEFAULT_HORIZON_DAYS = 30
DEFAULT_MODEL_VERSION = "latest"
DEFAULT_CONFIDENCE_LEVELS: Tuple[float, ...] = (0.1, 0.5, 0.9)

# Assumption bounds.
DISCOUNT_DEPTH_MIN = 0.0
DISCOUNT_DEPTH_MAX = 1.0
TRAFFIC_MULTIPLIER_MIN = 0.5
TRAFFIC_MULTIPLIER_MAX = 2.0
MAX_CLOSURE_DATES = MAX_HORIZON_DAYS

# Confidence levels are quantiles, so the degenerate 0.0 / 1.0 ends are excluded.
CONFIDENCE_LEVEL_MIN_EXCLUSIVE = 0.0
CONFIDENCE_LEVEL_MAX_EXCLUSIVE = 1.0


class ScenarioType(str, Enum):
    """Kind of what-if question being asked."""

    PROMOTION = "promotion"
    TRAFFIC = "traffic"
    CLOSURE = "closure"
    HOLIDAY = "holiday"
    PRICE = "price"
    CUSTOM = "custom"


class PromotionIntensity(str, Enum):
    """Qualitative promotion strength, mapped to multipliers by scenario_config."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HistoryPoint(BaseModel):
    """One observed store-day of history supplied by the caller."""

    model_config = ConfigDict(extra="forbid")

    date: date
    sales: float = Field(ge=0.0, description="Observed sales for the day; never negative.")
    store_id: Optional[str] = None
    customer_traffic: Optional[float] = Field(default=None, ge=0.0)
    is_promotion: Optional[bool] = None
    promotion_intensity: Optional[PromotionIntensity] = None
    price: Optional[float] = Field(default=None, gt=0.0)
    is_closed: Optional[bool] = None


class ScenarioAssumptions(BaseModel):
    """
    The levers a planner can pull.

    Which fields are required depends on ``scenario_type``; that per-type policy
    lives in ``api.services.scenario_config`` and is enforced by
    ``ScenarioValidationService``, not here, so the contract stays permissive
    enough to describe a partially filled form.

    ``duration_days`` is derived from the window when it is omitted and both
    ``start_date`` and ``end_date`` are present. When all three are supplied
    they must agree.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_type: ScenarioType
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    duration_days: Optional[int] = Field(default=None, ge=1, le=MAX_HORIZON_DAYS)
    promotion_intensity: PromotionIntensity = PromotionIntensity.NONE
    discount_depth: Optional[float] = Field(
        default=None,
        ge=DISCOUNT_DEPTH_MIN,
        le=DISCOUNT_DEPTH_MAX,
        description="Fractional discount, 0.0 to 1.0 (0.25 = 25% off).",
    )
    traffic_multiplier: Optional[float] = Field(
        default=None,
        ge=TRAFFIC_MULTIPLIER_MIN,
        le=TRAFFIC_MULTIPLIER_MAX,
        description="Multiplier applied to expected customer traffic.",
    )
    closure_dates: List[date] = Field(default_factory=list, max_length=MAX_CLOSURE_DATES)
    custom_overrides: Dict[str, Union[float, int, str]] = Field(default_factory=dict)

    @field_validator("closure_dates")
    @classmethod
    def _closure_dates_unique_and_sorted(cls, value: List[date]) -> List[date]:
        """Reject duplicate closure dates and normalise the order."""
        if len(set(value)) != len(value):
            duplicates = sorted({d for d in value if value.count(d) > 1})
            raise ValueError(
                "closure_dates must not contain duplicates: "
                f"{[d.isoformat() for d in duplicates]}"
            )
        return sorted(value)

    @model_validator(mode="after")
    def _check_window(self) -> "ScenarioAssumptions":
        """Enforce end_date >= start_date and keep duration_days consistent."""
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError(
                f"end_date ({self.end_date.isoformat()}) must be greater than or "
                f"equal to start_date ({self.start_date.isoformat()})"
            )

        if self.start_date and self.end_date:
            span = (self.end_date - self.start_date).days + 1
            if self.duration_days is None:
                self.duration_days = span
            elif self.duration_days != span:
                raise ValueError(
                    f"duration_days ({self.duration_days}) does not match the "
                    f"start_date..end_date span ({span} days)"
                )
        return self

    @property
    def window_dates(self) -> List[date]:
        """Every date in the scenario window, empty when the window is open-ended."""
        if not self.start_date or not self.end_date:
            return []
        span = (self.end_date - self.start_date).days + 1
        return [date.fromordinal(self.start_date.toordinal() + offset) for offset in range(span)]

    def affects(self, day: date) -> bool:
        """Whether ``day`` falls inside the scenario window or a closure date."""
        if day in self.closure_dates:
            return True
        if self.start_date and self.end_date:
            return self.start_date <= day <= self.end_date
        return False


class ScenarioRequest(BaseModel):
    """A scenario forecast request: history in, what-if levers, horizon out."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    store_id: str = Field(min_length=1)
    model_version: Optional[str] = DEFAULT_MODEL_VERSION
    horizon_days: int = Field(
        default=DEFAULT_HORIZON_DAYS,
        ge=MIN_HORIZON_DAYS,
        le=MAX_HORIZON_DAYS,
    )
    confidence_levels: List[float] = Field(
        default_factory=lambda: list(DEFAULT_CONFIDENCE_LEVELS)
    )
    history: List[HistoryPoint] = Field(min_length=MIN_HISTORY_ROWS)
    scenario: ScenarioAssumptions
    baseline_forecast_id: Optional[str] = None

    @field_validator("confidence_levels")
    @classmethod
    def _check_confidence_levels(cls, value: List[float]) -> List[float]:
        """Confidence levels must be non-empty, in (0, 1), unique and ascending."""
        if not value:
            raise ValueError("confidence_levels must not be empty")

        out_of_range = [
            level
            for level in value
            if not CONFIDENCE_LEVEL_MIN_EXCLUSIVE < level < CONFIDENCE_LEVEL_MAX_EXCLUSIVE
        ]
        if out_of_range:
            raise ValueError(
                f"confidence_levels must be strictly between 0 and 1, got {out_of_range}"
            )
        if len(set(value)) != len(value):
            raise ValueError(f"confidence_levels must be unique, got {value}")
        if list(value) != sorted(value):
            raise ValueError(f"confidence_levels must be sorted ascending, got {value}")
        return value

    @field_validator("history")
    @classmethod
    def _check_history_order(cls, value: List[HistoryPoint]) -> List[HistoryPoint]:
        """History dates must be unique and chronologically ascending."""
        dates = [point.date for point in value]
        if len(set(dates)) != len(dates):
            duplicates = sorted({d for d in dates if dates.count(d) > 1})
            raise ValueError(
                f"history dates must be unique, duplicates: "
                f"{[d.isoformat() for d in duplicates]}"
            )
        if dates != sorted(dates):
            raise ValueError("history must be sorted by date in ascending order")
        return value

    @model_validator(mode="after")
    def _check_closure_dates_not_in_past(self) -> "ScenarioRequest":
        """Closure dates may not precede the most recent observed history date."""
        if not self.history or not self.scenario.closure_dates:
            return self

        last_history_date = self.history[-1].date
        stale = [d for d in self.scenario.closure_dates if d < last_history_date]
        if stale:
            raise ValueError(
                "closure_dates must not be in the past relative to the latest "
                f"history date ({last_history_date.isoformat()}): "
                f"{[d.isoformat() for d in stale]}"
            )
        return self

    @property
    def last_history_date(self) -> Optional[date]:
        """Most recent observed date, or None when history is empty."""
        return self.history[-1].date if self.history else None


class ScenarioForecastPoint(BaseModel):
    """Baseline vs scenario prediction for a single forecast day."""

    model_config = ConfigDict(extra="forbid")

    date: date
    baseline_sales: float
    scenario_sales: float
    delta: float = Field(description="scenario_sales - baseline_sales.")
    delta_pct: Optional[float] = Field(
        default=None,
        description="Percent change vs baseline; None when baseline is zero.",
    )
    quantiles: Dict[str, float] = Field(
        default_factory=dict,
        description='Scenario quantiles keyed by confidence level, e.g. {"0.9": 512.0}.',
    )
    in_scenario_window: bool = False

    @classmethod
    def build(
        cls,
        *,
        day: date,
        baseline_sales: float,
        scenario_sales: float,
        quantiles: Optional[Dict[str, float]] = None,
        in_scenario_window: bool = False,
    ) -> "ScenarioForecastPoint":
        """Construct a point with ``delta`` and ``delta_pct`` derived consistently."""
        delta = scenario_sales - baseline_sales
        delta_pct = (delta / baseline_sales) * 100.0 if baseline_sales else None
        return cls(
            date=day,
            baseline_sales=baseline_sales,
            scenario_sales=scenario_sales,
            delta=delta,
            delta_pct=delta_pct,
            quantiles=quantiles or {},
            in_scenario_window=in_scenario_window,
        )


class ScenarioImpactSummary(BaseModel):
    """Aggregate impact of a scenario across the whole horizon."""

    model_config = ConfigDict(extra="forbid")

    horizon_days: int = Field(ge=0)
    baseline_total: float
    scenario_total: float
    absolute_delta: float
    percent_delta: Optional[float] = Field(
        default=None, description="None when the baseline total is zero."
    )
    average_daily_delta: float = 0.0
    peak_delta: float = 0.0
    peak_delta_date: Optional[date] = None
    days_in_scenario_window: int = Field(default=0, ge=0)

    @classmethod
    def from_points(cls, points: List[ScenarioForecastPoint]) -> "ScenarioImpactSummary":
        """Derive a summary from per-day points; safe on an empty horizon."""
        if not points:
            return cls(
                horizon_days=0,
                baseline_total=0.0,
                scenario_total=0.0,
                absolute_delta=0.0,
            )

        baseline_total = sum(point.baseline_sales for point in points)
        scenario_total = sum(point.scenario_sales for point in points)
        absolute_delta = scenario_total - baseline_total
        peak = max(points, key=lambda point: abs(point.delta))
        return cls(
            horizon_days=len(points),
            baseline_total=baseline_total,
            scenario_total=scenario_total,
            absolute_delta=absolute_delta,
            percent_delta=(
                (absolute_delta / baseline_total) * 100.0 if baseline_total else None
            ),
            average_daily_delta=absolute_delta / len(points),
            peak_delta=peak.delta,
            peak_delta_date=peak.date,
            days_in_scenario_window=sum(1 for point in points if point.in_scenario_window),
        )


class ScenarioComparisonResponse(BaseModel):
    """Full baseline-vs-scenario comparison returned to the caller."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    scenario_id: str
    store_id: str
    model_version: str
    scenario_type: ScenarioType
    horizon_days: int = Field(ge=0)
    confidence_levels: List[float] = Field(default_factory=list)
    assumptions: ScenarioAssumptions
    points: List[ScenarioForecastPoint] = Field(default_factory=list)
    summary: ScenarioImpactSummary
    baseline_forecast_id: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    schema_version: str = SCENARIO_SCHEMA_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScenarioCapability(BaseModel):
    """What one scenario type supports, for client-side form building."""

    model_config = ConfigDict(extra="forbid")

    scenario_type: ScenarioType
    supported: bool
    label: str
    description: str
    required_fields: List[str] = Field(default_factory=list)
    optional_fields: List[str] = Field(default_factory=list)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None


class ScenarioCapabilitiesResponse(BaseModel):
    """Advertised scenario-planning capabilities and request limits."""

    model_config = ConfigDict(extra="forbid")

    capabilities: List[ScenarioCapability] = Field(default_factory=list)
    schema_version: str = SCENARIO_SCHEMA_VERSION
    min_history_rows: int = MIN_HISTORY_ROWS
    min_horizon_days: int = MIN_HORIZON_DAYS
    max_horizon_days: int = MAX_HORIZON_DAYS
    default_horizon_days: int = DEFAULT_HORIZON_DAYS
    default_model_version: str = DEFAULT_MODEL_VERSION
    default_confidence_levels: List[float] = Field(
        default_factory=lambda: list(DEFAULT_CONFIDENCE_LEVELS)
    )

    @property
    def supported_scenario_types(self) -> List[ScenarioType]:
        """Scenario types currently enabled."""
        return [cap.scenario_type for cap in self.capabilities if cap.supported]


class ScenarioErrorResponse(BaseModel):
    """Structured error body, mirroring ScenarioValidationError."""

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: str
    field: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
