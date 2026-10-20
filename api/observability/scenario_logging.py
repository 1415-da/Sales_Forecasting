"""
Structured logging for the Scenario Planner.

Every scenario event is emitted as one JSON line carrying the same core fields,
so a log search can follow a single request through the whole pipeline and
aggregate by store, model, or scenario type without parsing prose.

Two rules hold throughout:

* **Nothing sensitive is logged.** Every string passes through
  ``sanitize_text``, which strips paths, connection strings, object-store keys,
  and internal hostnames. Request payloads -- which contain the caller's sales
  history -- are never logged, only their row counts.
* **Logging never breaks a request.** A failure inside an emitter is swallowed
  after a warning: losing a log line is bad, losing the scenario because of it
  is worse.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterator, Optional

from api.core.errors import normalize_error, sanitize_text

logger = logging.getLogger("scenario")

SCHEMA_VERSION = "v1"


class ScenarioEvent(str, Enum):
    """The scenario lifecycle, as emitted to logs."""

    REQUEST_RECEIVED = "scenario.request_received"
    VALIDATION_COMPLETED = "scenario.validation_completed"
    CAPABILITY_CHECK_COMPLETED = "scenario.capability_check_completed"
    BASELINE_CREATED = "scenario.baseline_created"
    BASELINE_REUSED = "scenario.baseline_reused"
    OVERRIDES_APPLIED = "scenario.overrides_applied"
    PREDICTION_COMPLETED = "scenario.prediction_completed"
    IMPACT_CALCULATED = "scenario.impact_calculated"
    SCENARIO_COMPLETED = "scenario.completed"
    SCENARIO_FAILED = "scenario.failed"
    SCENARIO_ROLLED_BACK = "scenario.rolled_back"


class EventStatus(str, Enum):
    """Outcome carried on every event."""

    STARTED = "started"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ScenarioLogContext:
    """
    The identity shared by every event of one scenario request.

    Built once per request and threaded through the pipeline, so no emitter has
    to re-derive which store or model it is talking about.
    """

    request_id: str
    store_id: str
    model_version: str
    scenario_type: str
    scenario_id: Optional[str] = None
    baseline_forecast_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def with_scenario_id(self, scenario_id: Optional[str]) -> "ScenarioLogContext":
        """A copy carrying the scenario id, once the backend has assigned one."""
        return ScenarioLogContext(
            request_id=self.request_id,
            store_id=self.store_id,
            model_version=self.model_version,
            scenario_type=self.scenario_type,
            scenario_id=scenario_id or self.scenario_id,
            baseline_forecast_id=self.baseline_forecast_id,
            extra=dict(self.extra),
        )

    def with_baseline(self, baseline_forecast_id: Optional[str]) -> "ScenarioLogContext":
        """A copy carrying the baseline id."""
        clone = self.with_scenario_id(self.scenario_id)
        clone.baseline_forecast_id = baseline_forecast_id or self.baseline_forecast_id
        return clone

    def to_fields(self) -> Dict[str, Any]:
        """The core fields, with None values dropped."""
        fields = asdict(self)
        extra = fields.pop("extra", {}) or {}
        merged = {**fields, **extra}
        return {key: value for key, value in merged.items() if value is not None}


def _emit(
    event: ScenarioEvent,
    context: Optional[ScenarioLogContext],
    status: EventStatus,
    duration_ms: Optional[int] = None,
    error_code: Optional[str] = None,
    message: Optional[str] = None,
    level: int = logging.INFO,
    **fields: Any,
) -> Dict[str, Any]:
    """
    Emit one structured event and return the record.

    The record is returned so callers (and tests) can assert on exactly what
    was logged, rather than parsing the formatted output.
    """
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event": event.value,
        "status": status.value,
    }
    if context is not None:
        record.update(context.to_fields())
    if duration_ms is not None:
        record["duration_ms"] = int(duration_ms)
    if error_code:
        record["error_code"] = error_code
    if message:
        record["message"] = sanitize_text(message)

    for key, value in fields.items():
        if value is not None:
            record[key] = sanitize_text(value) if isinstance(value, str) else value

    try:
        logger.log(level, json.dumps(record, default=str, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - logging must never break a request
        logger.warning("Could not emit scenario event %s: %s", event.value, exc)
    return record


# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------


def log_request_received(
    context: ScenarioLogContext,
    horizon_days: int,
    history_rows: int,
    idempotency_key_present: bool = False,
) -> Dict[str, Any]:
    """A comparison request arrived.

    Only the *shape* of the history is recorded. The series itself is the
    caller's business data and never belongs in a log.
    """
    return _emit(
        ScenarioEvent.REQUEST_RECEIVED,
        context,
        EventStatus.STARTED,
        horizon_days=horizon_days,
        history_rows=history_rows,
        idempotent=idempotency_key_present,
    )


def log_validation_completed(
    context: ScenarioLogContext,
    duration_ms: int,
    ok: bool = True,
    error_code: Optional[str] = None,
    field_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Request validation finished."""
    return _emit(
        ScenarioEvent.VALIDATION_COMPLETED,
        context,
        EventStatus.OK if ok else EventStatus.FAILED,
        duration_ms=duration_ms,
        error_code=error_code,
        level=logging.INFO if ok else logging.WARNING,
        field=field_name,
    )


def log_capability_check_completed(
    context: ScenarioLogContext,
    duration_ms: int,
    supported: bool,
    missing_features: Optional[list] = None,
    error_code: Optional[str] = None,
) -> Dict[str, Any]:
    """The model's capability for this scenario type was resolved."""
    return _emit(
        ScenarioEvent.CAPABILITY_CHECK_COMPLETED,
        context,
        EventStatus.OK if supported else EventStatus.FAILED,
        duration_ms=duration_ms,
        error_code=error_code,
        level=logging.INFO if supported else logging.WARNING,
        supported=supported,
        missing_features=missing_features or None,
    )


def log_baseline_resolved(
    context: ScenarioLogContext,
    duration_ms: int,
    reused: bool,
    baseline_forecast_id: Optional[str] = None,
) -> Dict[str, Any]:
    """A baseline was generated, or an existing one reused."""
    return _emit(
        ScenarioEvent.BASELINE_REUSED if reused else ScenarioEvent.BASELINE_CREATED,
        context.with_baseline(baseline_forecast_id),
        EventStatus.OK,
        duration_ms=duration_ms,
        reused=reused,
    )


def log_overrides_applied(
    context: ScenarioLogContext,
    duration_ms: int,
    applied_features: Optional[list] = None,
    unsupported_features: Optional[list] = None,
    modified_days: int = 0,
) -> Dict[str, Any]:
    """Scenario overrides were written onto the future feature matrix."""
    unsupported = unsupported_features or []
    return _emit(
        ScenarioEvent.OVERRIDES_APPLIED,
        context,
        EventStatus.OK if not unsupported else EventStatus.SKIPPED,
        duration_ms=duration_ms,
        level=logging.INFO if not unsupported else logging.WARNING,
        applied_features=applied_features or [],
        unsupported_features=unsupported or None,
        modified_days=modified_days,
        partial=bool(unsupported),
    )


def log_prediction_completed(
    context: ScenarioLogContext,
    duration_ms: int,
    horizon_days: int,
    ok: bool = True,
    error_code: Optional[str] = None,
    has_quantiles: Optional[bool] = None,
) -> Dict[str, Any]:
    """Both sides of the comparison were predicted."""
    return _emit(
        ScenarioEvent.PREDICTION_COMPLETED,
        context,
        EventStatus.OK if ok else EventStatus.FAILED,
        duration_ms=duration_ms,
        error_code=error_code,
        level=logging.INFO if ok else logging.ERROR,
        horizon_days=horizon_days,
        has_quantiles=has_quantiles,
    )


def log_impact_calculated(
    context: ScenarioLogContext,
    duration_ms: int,
    total_difference: Optional[float] = None,
    total_difference_pct: Optional[float] = None,
    positive_days: Optional[int] = None,
    negative_days: Optional[int] = None,
) -> Dict[str, Any]:
    """The impact figures were derived from the two series."""
    return _emit(
        ScenarioEvent.IMPACT_CALCULATED,
        context,
        EventStatus.OK,
        duration_ms=duration_ms,
        total_difference=total_difference,
        total_difference_pct=total_difference_pct,
        positive_days=positive_days,
        negative_days=negative_days,
    )


def log_scenario_completed(
    context: ScenarioLogContext,
    duration_ms: int,
    baseline_reused: bool = False,
    partial: bool = False,
) -> Dict[str, Any]:
    """A comparison finished successfully."""
    return _emit(
        ScenarioEvent.SCENARIO_COMPLETED,
        context,
        EventStatus.OK,
        duration_ms=duration_ms,
        baseline_reused=baseline_reused,
        partial=partial,
    )


def log_scenario_failed(
    context: ScenarioLogContext,
    exc: BaseException,
    duration_ms: Optional[int] = None,
    stage: Optional[str] = None,
) -> Dict[str, Any]:
    """
    A comparison failed.

    The error is normalised first, so the logged ``error_code`` is the same
    taxonomy code the client received -- which is what makes a user's support
    request traceable to a log line.
    """
    error = normalize_error(exc)
    return _emit(
        ScenarioEvent.SCENARIO_FAILED,
        context,
        EventStatus.FAILED,
        duration_ms=duration_ms,
        error_code=error.error_code,
        message=error.message,
        level=logging.ERROR if error.is_server_error else logging.WARNING,
        stage=stage,
        http_status=error.status_code,
    )


def log_scenario_rolled_back(
    context: ScenarioLogContext,
    reason: str,
    duration_ms: Optional[int] = None,
    audit_preserved: bool = True,
    baseline_preserved: bool = True,
) -> Dict[str, Any]:
    """A scenario was withdrawn."""
    return _emit(
        ScenarioEvent.SCENARIO_ROLLED_BACK,
        context,
        EventStatus.OK,
        duration_ms=duration_ms,
        reason=reason,
        audit_preserved=audit_preserved,
        baseline_preserved=baseline_preserved,
    )


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------


class StageTimer:
    """Measures elapsed milliseconds for one pipeline stage."""

    def __init__(self) -> None:
        self._started = time.perf_counter()

    def elapsed_ms(self) -> int:
        """Milliseconds since the timer was created."""
        return int((time.perf_counter() - self._started) * 1000)

    def reset(self) -> None:
        """Restart the timer, for the next stage."""
        self._started = time.perf_counter()


@contextmanager
def stage_timer() -> Iterator[StageTimer]:
    """Time a block of pipeline work."""
    timer = StageTimer()
    yield timer


def configure_json_logging(level: int = logging.INFO) -> None:
    """
    Send the ``scenario`` logger's records to stdout as-is.

    The emitters already produce JSON, so the formatter must not wrap them in
    another layout. Called from the app factory; safe to call twice.
    """
    scenario_logger = logging.getLogger("scenario")
    scenario_logger.setLevel(level)
    if any(getattr(h, "_scenario_json", False) for h in scenario_logger.handlers):
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    setattr(handler, "_scenario_json", True)
    scenario_logger.addHandler(handler)
    # Records are already structured; re-emitting them through the root logger
    # would duplicate every line.
    scenario_logger.propagate = False


__all__ = [
    "EventStatus",
    "SCHEMA_VERSION",
    "ScenarioEvent",
    "ScenarioLogContext",
    "StageTimer",
    "configure_json_logging",
    "log_baseline_resolved",
    "log_capability_check_completed",
    "log_impact_calculated",
    "log_overrides_applied",
    "log_prediction_completed",
    "log_request_received",
    "log_scenario_completed",
    "log_scenario_failed",
    "log_scenario_rolled_back",
    "log_validation_completed",
    "stage_timer",
]
