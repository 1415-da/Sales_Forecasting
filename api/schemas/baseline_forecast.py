"""
Baseline forecast contract for the Scenario Planner.

A baseline is the "do nothing" forecast a scenario is compared against. These
models describe the response, its per-day rows, and the persisted checkpoint
metadata. No inference, persistence, or routing happens here.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.schemas.scenario import DEFAULT_CONFIDENCE_LEVELS, SCENARIO_SCHEMA_VERSION

BASELINE_FORECAST_ID_PREFIX = "bf_"
UNKNOWN_LINEAGE_VALUE = "unknown"


class UncertaintyMethod(str, Enum):
    """How the forecast interval was produced."""

    QUANTILE_MODEL = "quantile_model"  # native quantile predictions from the model
    RESIDUAL_BASED = "residual_based"  # approximated from residual spread


class BaselineForecastStatus(str, Enum):
    """Lifecycle of a persisted baseline forecast checkpoint."""

    CREATED = "created"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class ModelStage(str, Enum):
    """MLflow registry stage of the model backing a baseline."""

    PRODUCTION = "Production"
    STAGING = "Staging"
    ARCHIVED = "Archived"
    NONE = "None"


class BaselineDailyForecast(BaseModel):
    """One forecast day of the baseline."""

    model_config = ConfigDict(extra="forbid")

    date: date
    predicted_sales: float
    lower_bound: float
    upper_bound: float
    p10: Optional[float] = None
    p50: Optional[float] = None
    p90: Optional[float] = None
    uncertainty_method: UncertaintyMethod = UncertaintyMethod.RESIDUAL_BASED

    @model_validator(mode="after")
    def _check_bounds_ordered(self) -> "BaselineDailyForecast":
        """Bounds must bracket the point forecast."""
        if self.lower_bound > self.upper_bound:
            raise ValueError(
                f"lower_bound ({self.lower_bound}) must not exceed "
                f"upper_bound ({self.upper_bound})"
            )
        return self

    @property
    def interval_width(self) -> float:
        """Width of the prediction interval."""
        return self.upper_bound - self.lower_bound


class BaselineForecastSummary(BaseModel):
    """Aggregate view of a baseline forecast."""

    model_config = ConfigDict(extra="forbid")

    horizon_days: int = Field(ge=0)
    total_predicted_sales: float = 0.0
    average_daily_sales: float = 0.0
    min_daily_sales: float = 0.0
    max_daily_sales: float = 0.0
    peak_date: Optional[date] = None
    average_interval_width: float = 0.0

    @classmethod
    def from_daily(cls, daily: List[BaselineDailyForecast]) -> "BaselineForecastSummary":
        """Derive a summary from per-day rows; safe on an empty horizon."""
        if not daily:
            return cls(horizon_days=0)

        values = [row.predicted_sales for row in daily]
        peak = max(daily, key=lambda row: row.predicted_sales)
        return cls(
            horizon_days=len(daily),
            total_predicted_sales=sum(values),
            average_daily_sales=sum(values) / len(values),
            min_daily_sales=min(values),
            max_daily_sales=max(values),
            peak_date=peak.date,
            average_interval_width=sum(row.interval_width for row in daily) / len(daily),
        )


class BaselineForecastResponse(BaseModel):
    """A reusable baseline forecast plus the lineage needed to interpret it."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    baseline_forecast_id: str
    store_id: str
    #: Model family the baseline came from (``ensemble``, ``xgboost``, ...).
    model: Optional[str] = None
    model_version: str
    model_stage: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    horizon_days: int = Field(ge=1)
    history_start_date: date
    history_end_date: date
    forecast_start_date: date
    forecast_end_date: date
    daily_forecasts: List[BaselineDailyForecast] = Field(default_factory=list)
    summary: BaselineForecastSummary
    feature_schema_version: str
    data_source: str = UNKNOWN_LINEAGE_VALUE
    dataset_version: str = UNKNOWN_LINEAGE_VALUE

    uncertainty_method: UncertaintyMethod = UncertaintyMethod.RESIDUAL_BASED
    confidence_levels: List[float] = Field(
        default_factory=lambda: list(DEFAULT_CONFIDENCE_LEVELS)
    )
    run_id: Optional[str] = None
    status: BaselineForecastStatus = BaselineForecastStatus.COMPLETED
    warnings: List[str] = Field(default_factory=list)
    schema_version: str = SCENARIO_SCHEMA_VERSION


class BaselineForecastCheckpoint(BaseModel):
    """Persisted metadata for a baseline forecast (one DB row)."""

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    forecast_id: str
    store_id: str
    #: Model family; ``None`` on rows written before it was recorded, which
    #: were all produced by the default model.
    model: Optional[str] = None
    model_version: str
    model_stage: str
    horizon_days: int
    history_start_date: date
    history_end_date: date
    forecast_start_date: date
    forecast_end_date: date
    data_source: str
    dataset_version: str
    feature_schema_version: str
    status: BaselineForecastStatus
    created_at: datetime
    expires_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    id: Optional[Any] = None

    @property
    def is_expired(self) -> bool:
        """Whether this checkpoint has passed its expiry."""
        if self.expires_at is None:
            return False
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)

    @property
    def is_reusable(self) -> bool:
        """Whether a scenario may compare against this checkpoint."""
        return self.status is BaselineForecastStatus.COMPLETED and not self.is_expired


class BaselineForecastErrorResponse(BaseModel):
    """Structured error body for baseline forecast failures."""

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: str
    field: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
