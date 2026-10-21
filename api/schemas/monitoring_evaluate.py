"""
Contract for the monitoring evaluation run.

The evaluation lives behind an endpoint rather than in the Airflow DAG because
of a hard dependency conflict: Airflow 3 pins SQLAlchemy 1.4, while the API's
models use the 2.0 declarative API. A DAG that imported ``api.db`` would fail
at import. Running the evaluation inside the API process keeps one
implementation, one dependency set, and one place where monitoring is defined;
the DAG becomes an orchestrator that triggers it on a schedule.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class EvaluateRequest(BaseModel):
    """Parameters for one evaluation pass."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    #: Days of actuals to (re)load. More than one because late corrections to
    #: recent days are normal and a single-day window would never see them.
    actuals_lookback_days: int = Field(default=14, ge=1, le=365)
    #: Accuracy evaluation window.
    window_days: int = Field(default=28, ge=1, le=365)
    #: Resolve alerts whose condition has not recurred for this long.
    alert_stale_days: int = Field(default=30, ge=1, le=365)
    #: Score against this version; defaults to the serving Production version.
    model_version: Optional[str] = None
    #: Skip the actuals load, to re-score what is already stored.
    skip_actuals: bool = False
    #: Treat this as "today". For backfilling a historical evaluation.
    reference_date: Optional[date] = None


class ActualsLoadResult(BaseModel):
    """Outcome of the actuals ingestion step."""

    model_config = ConfigDict(extra="forbid")

    loaded: int = 0
    inserted: int = 0
    updated: int = 0
    files_read: int = 0
    date_min: Optional[date] = None
    date_max: Optional[date] = None
    #: Present when nothing could be loaded, naming why.
    skipped_reason: Optional[str] = None


class EvaluateResponse(BaseModel):
    """
    What one evaluation produced.

    Deliberately reports zeros and a reason rather than failing when there is
    nothing to score: "no forecast has reached its outcome date yet" is the
    normal state of a freshly deployed platform, not an error.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    success: bool = True
    status: str
    model_version: str
    window_start: date
    window_end: date

    actuals: ActualsLoadResult
    scored_pairs: int = 0
    metrics_written: int = 0
    metrics: Dict[str, float] = Field(default_factory=dict)

    drift_evaluated: int = 0
    drift_detected: int = 0
    drift_skipped_reason: Optional[str] = None

    alerts_created: int = 0
    alerts_refreshed: int = 0
    alerts_auto_resolved: int = 0
    alert_titles: List[str] = Field(default_factory=list)

    recommendation: str = "no_data"
    recommendation_reason: str = ""
    no_data_message: Optional[str] = None


__all__ = ["ActualsLoadResult", "EvaluateRequest", "EvaluateResponse"]
