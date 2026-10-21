"""
Request and response contracts for ``POST /v1/forecast``.

A separate history point type from ``api.schemas.scenario.HistoryPoint``: that
one describes scenario levers (price, closure, promotion intensity), this one
describes an observed store-day plus the optional exogenous columns the trained
feature schema actually contains. Conflating them would let a caller send a
field the model has no slot for.

All validation that can be expressed structurally lives here, so a malformed
request is rejected before any model is loaded.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Horizons the UI offers. Any integer in range is accepted; these are the
#: presets the frontend renders.
HORIZON_PRESETS = (7, 14, 30, 60, 90)

MIN_HORIZON_DAYS = 1
MAX_HORIZON_DAYS = 90

#: Uncertainty is a band around a point forecast, not a model-native quantile.
UNCERTAINTY_METHOD_RESIDUAL = "residual_normal"
UNCERTAINTY_METHOD_HISTORY = "history_volatility"

#: Version of the inference-side feature contract. Bumped when the derivation
#: of any model input changes, so a stored forecast records how it was built.
FEATURE_SCHEMA_VERSION = "v1"


class HistoryObservation(BaseModel):
    """
    One observed store-day.

    ``sales`` is the only required measure. The remaining fields exist in the
    trained feature schema; supplying them replaces the reconstruction the
    service would otherwise do, and their absence is reported in
    ``metadata.imputed_features``.

    There is no ``is_holiday`` field: the training pipeline derives that column
    from the US public-holiday calendar, so the service reproduces it for each
    forecast date and a caller-supplied value could never reach the model.
    """

    model_config = ConfigDict(extra="forbid")

    date: date
    sales: float = Field(ge=0.0, description="Observed sales for the day.")
    store_id: Optional[str] = Field(
        default=None, description="Overrides the request-level store for this day."
    )
    quantity_sold: Optional[float] = Field(default=None, ge=0.0)
    profit: Optional[float] = None
    customer_traffic: Optional[float] = Field(default=None, ge=0.0)
    has_promotion: Optional[bool] = None


class ForecastRequest(BaseModel):
    """A request for a multi-day forecast from one model version."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    store_id: str = Field(min_length=1, max_length=64)
    model: str = Field(
        default="ensemble",
        description="Short model name: ensemble, xgboost, or lightgbm.",
    )
    model_version: str = Field(
        default="latest",
        description="Registry version number, or 'latest' for the newest Production version.",
    )
    horizon_days: int = Field(default=30, ge=MIN_HORIZON_DAYS, le=MAX_HORIZON_DAYS)
    confidence_level: float = Field(default=0.95, gt=0.0, lt=1.0)
    history: List[HistoryObservation] = Field(min_length=1)

    @field_validator("store_id", "model", "model_version")
    @classmethod
    def _strip(cls, value: str) -> str:
        """Trim incidental whitespace from identifiers."""
        return value.strip()

    @field_validator("model")
    @classmethod
    def _lower_model(cls, value: str) -> str:
        """Model names are lowercase by convention."""
        return value.lower()

    @field_validator("history")
    @classmethod
    def _chronological_and_unique(
        cls, value: List[HistoryObservation]
    ) -> List[HistoryObservation]:
        """
        History must be strictly increasing in date.

        Lag and rolling features are positional, so a duplicated or out-of-order
        day would silently shift every window. Sorting on the caller's behalf
        would hide a genuine data problem, so this is an error.
        """
        seen: set = set()
        previous: Optional[date] = None
        for point in value:
            if point.date in seen:
                raise ValueError(f"duplicate history date: {point.date.isoformat()}")
            if previous is not None and point.date < previous:
                raise ValueError(
                    "history must be ordered oldest-first by date; "
                    f"{point.date.isoformat()} follows {previous.isoformat()}"
                )
            seen.add(point.date)
            previous = point.date
        return value

    @model_validator(mode="after")
    def _confidence_is_offered(self) -> "ForecastRequest":
        """Only the confidence levels the uncertainty method is calibrated for."""
        if round(self.confidence_level, 4) not in (0.8, 0.95):
            raise ValueError("confidence_level must be 0.8 or 0.95")
        return self

    @property
    def history_start_date(self) -> date:
        """First observed day."""
        return self.history[0].date

    @property
    def history_end_date(self) -> date:
        """Last observed day; the forecast starts the day after."""
        return self.history[-1].date


class DailyForecast(BaseModel):
    """One forecast day with its uncertainty band."""

    model_config = ConfigDict(extra="forbid")

    date: date
    predicted_sales: float
    lower_bound: float
    upper_bound: float


class ForecastSummary(BaseModel):
    """Totals across the whole horizon."""

    model_config = ConfigDict(extra="forbid")

    total_forecast_sales: float
    average_daily_sales: float
    lower_bound_total: float
    upper_bound_total: float


class ForecastMetadata(BaseModel):
    """
    How the forecast was produced.

    Enough to reproduce or audit it: the history window consumed, the feature
    contract version, the uncertainty method, and which model inputs had to be
    reconstructed because the caller could not know them for a future date.
    """

    model_config = ConfigDict(extra="forbid")

    history_start_date: str
    history_end_date: str
    history_days: int
    feature_schema_version: str = FEATURE_SCHEMA_VERSION
    feature_count: int = 0
    uncertainty_method: str = UNCERTAINTY_METHOD_RESIDUAL
    residual_std: Optional[float] = None
    imputed_features: List[str] = Field(default_factory=list)
    run_id: Optional[str] = None
    data_source: Optional[str] = None
    dataset_version: Optional[str] = None


class ForecastResponse(BaseModel):
    """The forecast, its summary, and the provenance needed to trust it."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    forecast_id: str
    store_id: str
    model: str
    model_version: str
    model_stage: str
    generated_at: datetime
    horizon_days: int
    confidence_level: float
    summary: ForecastSummary
    forecasts: List[DailyForecast]
    metadata: ForecastMetadata
    #: Caveats that apply to this specific forecast. Never empty in practice:
    #: the trained model's reliance on same-day columns is always disclosed.
    warnings: List[str] = Field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        """JSON-ready dict with dates rendered as ISO strings."""
        return self.model_dump(mode="json")


def utc_now() -> datetime:
    """Timezone-aware current time, for ``generated_at``."""
    return datetime.now(timezone.utc)


__all__ = [
    "FEATURE_SCHEMA_VERSION",
    "HORIZON_PRESETS",
    "MAX_HORIZON_DAYS",
    "MIN_HORIZON_DAYS",
    "UNCERTAINTY_METHOD_HISTORY",
    "UNCERTAINTY_METHOD_RESIDUAL",
    "DailyForecast",
    "ForecastMetadata",
    "ForecastRequest",
    "ForecastResponse",
    "ForecastSummary",
    "HistoryObservation",
    "utc_now",
]
