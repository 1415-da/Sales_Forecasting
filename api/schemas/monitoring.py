"""
Monitoring response contracts.

Every numeric field is optional. That is the whole design: a monitoring API
that types WAPE as ``float`` has to send *something* when nothing has been
scored, and whatever it sends will be believed. ``None`` plus an explicit
``has_data`` flag is the only honest shape.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from api.services.monitoring_service import NO_DATA_MESSAGE


class StoreCoverage(BaseModel):
    """Per-store actuals coverage."""

    model_config = ConfigDict(extra="forbid")

    store_id: str
    latest_date: Optional[date] = None
    days: int = 0


class FreshnessResponse(BaseModel):
    """How current the monitoring inputs are."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    status: str
    has_data: bool
    latest_actual_date: Optional[date] = None
    latest_forecast_date: Optional[date] = None
    latest_forecast_created_at: Optional[datetime] = None
    days_since_actual: Optional[int] = None
    actual_count: int = 0
    forecast_count: int = 0
    coverage: List[StoreCoverage] = Field(default_factory=list)
    no_data_message: Optional[str] = None


class AccuracyMetrics(BaseModel):
    """
    One window's accuracy.

    ``sample_size`` is mandatory and the metrics are not: a WAPE computed from
    three store-days should not be read like one computed from three hundred,
    so the count always travels with the value.
    """

    model_config = ConfigDict(extra="forbid")

    sample_size: int
    wape: Optional[float] = None
    mae: Optional[float] = None
    rmse: Optional[float] = None
    bias: Optional[float] = None
    mean_error_pct: Optional[float] = None
    mape: Optional[float] = None
    interval_coverage: Optional[float] = None
    r2: Optional[float] = None


class AccuracyResponse(BaseModel):
    """Current accuracy, the previous window, and the trend between them."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    has_data: bool
    window_days: int
    window_start: date
    window_end: date
    model_version: Optional[str] = None
    store_id: Optional[str] = None
    current: AccuracyMetrics
    previous: Optional[AccuracyMetrics] = None
    #: Fractional change per metric. Positive means the value grew, which is a
    #: regression for an error metric and an improvement for coverage.
    trend: Dict[str, Optional[float]] = Field(default_factory=dict)
    series: List[Dict[str, Any]] = Field(default_factory=list)
    no_data_message: Optional[str] = None


class DriftRow(BaseModel):
    """One feature's drift verdict."""

    model_config = ConfigDict(extra="forbid")

    feature_name: str
    drift_method: str
    drift_score: float
    threshold: float
    status: str
    model_version: Optional[str] = None
    reference_size: Optional[int] = None
    current_size: Optional[int] = None
    evaluation_date: Optional[date] = None


class DriftResponse(BaseModel):
    """The drift table."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    has_data: bool
    model_version: Optional[str] = None
    features: List[DriftRow] = Field(default_factory=list)
    drifting_features: int = 0
    warning_features: int = 0
    no_data_message: Optional[str] = None


class AlertResponse(BaseModel):
    """One alert."""

    model_config = ConfigDict(extra="forbid")

    id: str
    alert_type: str
    severity: str
    status: str
    title: str
    message: str
    recommended_action: Optional[str] = None
    model_version: Optional[str] = None
    store_id: Optional[str] = None
    occurrence_count: int = 1
    details: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    last_seen_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolution_note: Optional[str] = None

    @classmethod
    def from_row(cls, alert: Any) -> "AlertResponse":
        """Project an ``Alert`` row."""
        return cls(
            id=str(alert.id),
            alert_type=alert.alert_type,
            severity=alert.severity,
            status=alert.status,
            title=alert.title,
            message=alert.message,
            recommended_action=alert.recommended_action,
            model_version=alert.model_version,
            store_id=alert.store_id,
            occurrence_count=int(alert.occurrence_count or 1),
            details=dict(alert.details or {}),
            created_at=alert.created_at,
            last_seen_at=alert.last_seen_at,
            acknowledged_at=alert.acknowledged_at,
            resolved_at=alert.resolved_at,
            resolution_note=alert.resolution_note,
        )


class AlertListResponse(BaseModel):
    """A page of alerts."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    alerts: List[AlertResponse] = Field(default_factory=list)
    total: int = 0
    open_count: int = 0
    critical_count: int = 0
    warning_count: int = 0


class ResolveAlertRequest(BaseModel):
    """Optional note when resolving an alert."""

    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(default=None, max_length=2000)


class RetrainingRecommendationResponse(BaseModel):
    """What to do about the Production model, and why."""

    model_config = ConfigDict(extra="forbid")

    recommendation: str
    reason: str
    model_version: Optional[str] = None
    last_training_date: Optional[str] = None
    wape: Optional[float] = None
    wape_trend: Optional[float] = None
    drift_features: int = 0


class MonitoringSummaryResponse(BaseModel):
    """
    The dashboard's header, in one read.

    Deliberately one endpoint rather than five: the health verdict is a fold
    over all of the parts, and computing it client-side from separate calls
    would let the pieces disagree mid-render.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    status: str
    has_data: bool
    window_days: int
    window_start: date
    window_end: date
    model_version: Optional[str] = None
    freshness: FreshnessResponse
    accuracy: AccuracyResponse
    drift_summary: Dict[str, int] = Field(default_factory=dict)
    alert_summary: Dict[str, int] = Field(default_factory=dict)
    retraining: RetrainingRecommendationResponse
    #: Present only when there is genuinely nothing to show.
    no_data_message: Optional[str] = None
    #: Capabilities the caller holds, so the UI can hide what it cannot do.
    can_manage_alerts: bool = False
    can_view_detail: bool = False


NO_DATA = NO_DATA_MESSAGE

__all__ = [
    "NO_DATA",
    "AccuracyMetrics",
    "AccuracyResponse",
    "AlertListResponse",
    "AlertResponse",
    "DriftResponse",
    "DriftRow",
    "FreshnessResponse",
    "MonitoringSummaryResponse",
    "ResolveAlertRequest",
    "RetrainingRecommendationResponse",
    "StoreCoverage",
]
