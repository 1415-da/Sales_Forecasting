"""
Scenario planning business-rule validation.

Pydantic enforces the *shape* of a request. This service enforces the *rules*
and, critically, reports them as a structured ``ScenarioValidationError`` so a
future route layer can return a ``ScenarioErrorResponse`` without reaching into
Pydantic internals.

Every method takes primitives rather than a parsed ``ScenarioRequest``, so rules
can be applied before a request is assembled (for example against a partially
filled planning form), and so the checks remain correct for objects built via
``model_construct`` that bypassed field validation.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from api.schemas.scenario import (
    MAX_HORIZON_DAYS,
    MIN_HISTORY_ROWS,
    MIN_HORIZON_DAYS,
    HistoryPoint,
    ScenarioAssumptions,
    ScenarioErrorResponse,
    ScenarioRequest,
    ScenarioType,
)
from api.services.scenario_config import (
    get_enabled_scenario_types,
    get_required_fields,
)

logger = logging.getLogger(__name__)


class ScenarioErrorCode(str, Enum):
    """Stable machine-readable error codes for scenario validation failures."""

    HISTORY_EMPTY = "history_empty"
    HISTORY_TOO_SHORT = "history_too_short"
    HISTORY_DUPLICATE_DATES = "history_duplicate_dates"
    HISTORY_NOT_SORTED = "history_not_sorted"
    HISTORY_MISSING_DATE = "history_missing_date"
    HISTORY_NEGATIVE_SALES = "history_negative_sales"
    HISTORY_INVALID_SALES = "history_invalid_sales"

    INVALID_DATE_RANGE = "invalid_date_range"
    INVALID_DURATION = "invalid_duration"
    DURATION_MISMATCH = "duration_mismatch"
    MISSING_SCENARIO_FIELD = "missing_scenario_field"
    CLOSURE_DATE_IN_PAST = "closure_date_in_past"
    DUPLICATE_CLOSURE_DATES = "duplicate_closure_dates"

    CONFIDENCE_LEVELS_EMPTY = "confidence_levels_empty"
    CONFIDENCE_LEVEL_OUT_OF_RANGE = "confidence_level_out_of_range"
    CONFIDENCE_LEVELS_NOT_UNIQUE = "confidence_levels_not_unique"
    CONFIDENCE_LEVELS_NOT_SORTED = "confidence_levels_not_sorted"

    UNSUPPORTED_SCENARIO_TYPE = "unsupported_scenario_type"
    INVALID_HORIZON = "invalid_horizon"


class ScenarioValidationError(Exception):
    """
    A scenario request violated a business rule.

    Carries ``error_code``, ``message``, ``field`` and ``details`` so the caller
    can render a precise, field-anchored error without string parsing.
    """

    def __init__(
        self,
        error_code: ScenarioErrorCode | str,
        message: str,
        field: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_code: str = (
            error_code.value if isinstance(error_code, ScenarioErrorCode) else error_code
        )
        self.message = message
        self.field = field
        self.details: Dict[str, Any] = details or {}
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict form of the error."""
        return {
            "error_code": self.error_code,
            "message": self.message,
            "field": self.field,
            "details": self.details,
        }

    def to_response(self) -> ScenarioErrorResponse:
        """Schema form, ready to serialise as an error body."""
        return ScenarioErrorResponse(**self.to_dict())

    def __repr__(self) -> str:
        return (
            f"ScenarioValidationError(error_code={self.error_code!r}, "
            f"field={self.field!r}, message={self.message!r})"
        )


class ScenarioValidationService:
    """
    Stateless validator for scenario planning requests.

    Limits default to the contract constants but are injectable so tests and
    deployments can tighten them without editing the schema.
    """

    def __init__(
        self,
        min_history_rows: int = MIN_HISTORY_ROWS,
        min_horizon_days: int = MIN_HORIZON_DAYS,
        max_horizon_days: int = MAX_HORIZON_DAYS,
        enabled_scenario_types: Optional[Sequence[ScenarioType]] = None,
    ) -> None:
        self.min_history_rows = min_history_rows
        self.min_horizon_days = min_horizon_days
        self.max_horizon_days = max_horizon_days
        self._enabled_scenario_types: Optional[Tuple[ScenarioType, ...]] = (
            tuple(enabled_scenario_types) if enabled_scenario_types is not None else None
        )

    @property
    def enabled_scenario_types(self) -> Tuple[ScenarioType, ...]:
        """Enabled types, resolved from config when not injected."""
        if self._enabled_scenario_types is not None:
            return self._enabled_scenario_types
        return get_enabled_scenario_types()

    def validate_history(self, history: Sequence[HistoryPoint]) -> None:
        """
        Validate the supplied history series.

        Rules: non-empty, at least ``min_history_rows`` rows, every row dated,
        dates unique and strictly ascending, and sales finite and non-negative.
        """
        if not history:
            raise ScenarioValidationError(
                ScenarioErrorCode.HISTORY_EMPTY,
                "history must not be empty",
                field="history",
                details={"min_history_rows": self.min_history_rows, "received": 0},
            )

        if len(history) < self.min_history_rows:
            raise ScenarioValidationError(
                ScenarioErrorCode.HISTORY_TOO_SHORT,
                f"history must contain at least {self.min_history_rows} rows, "
                f"received {len(history)}",
                field="history",
                details={
                    "min_history_rows": self.min_history_rows,
                    "received": len(history),
                },
            )

        missing = [index for index, point in enumerate(history) if point.date is None]
        if missing:
            raise ScenarioValidationError(
                ScenarioErrorCode.HISTORY_MISSING_DATE,
                "every history row must have a date",
                field="history",
                details={"indexes": missing},
            )

        dates = [point.date for point in history]
        if len(set(dates)) != len(dates):
            duplicates = sorted({day for day in dates if dates.count(day) > 1})
            raise ScenarioValidationError(
                ScenarioErrorCode.HISTORY_DUPLICATE_DATES,
                "history dates must be unique",
                field="history",
                details={"duplicate_dates": [day.isoformat() for day in duplicates]},
            )

        if dates != sorted(dates):
            first_break = next(
                index
                for index in range(1, len(dates))
                if dates[index] < dates[index - 1]
            )
            raise ScenarioValidationError(
                ScenarioErrorCode.HISTORY_NOT_SORTED,
                "history must be sorted by date in ascending order",
                field="history",
                details={
                    "first_out_of_order_index": first_break,
                    "previous_date": dates[first_break - 1].isoformat(),
                    "date": dates[first_break].isoformat(),
                },
            )

        invalid = [
            index
            for index, point in enumerate(history)
            if point.sales is None or not math.isfinite(float(point.sales))
        ]
        if invalid:
            raise ScenarioValidationError(
                ScenarioErrorCode.HISTORY_INVALID_SALES,
                "history sales must be finite numbers",
                field="history",
                details={"indexes": invalid},
            )

        negative = [
            {"index": index, "date": point.date.isoformat(), "sales": float(point.sales)}
            for index, point in enumerate(history)
            if float(point.sales) < 0
        ]
        if negative:
            raise ScenarioValidationError(
                ScenarioErrorCode.HISTORY_NEGATIVE_SALES,
                "history sales must not be negative",
                field="history",
                details={"violations": negative},
            )

    def validate_scenario_dates(
        self,
        scenario: ScenarioAssumptions,
        last_history_date: Optional[date] = None,
    ) -> None:
        """
        Validate the scenario window, duration, and closure dates.

        Rules: ``end_date >= start_date``; a positive duration consistent with
        the window; the per-type required fields from ``scenario_config`` are
        populated; closure dates are unique and never precede
        ``last_history_date``.
        """
        start, end = scenario.start_date, scenario.end_date

        if start and end and end < start:
            raise ScenarioValidationError(
                ScenarioErrorCode.INVALID_DATE_RANGE,
                "end_date must be greater than or equal to start_date",
                field="scenario.end_date",
                details={"start_date": start.isoformat(), "end_date": end.isoformat()},
            )

        if scenario.duration_days is not None:
            if scenario.duration_days < 1:
                raise ScenarioValidationError(
                    ScenarioErrorCode.INVALID_DURATION,
                    "duration_days must be positive",
                    field="scenario.duration_days",
                    details={"duration_days": scenario.duration_days},
                )
            if scenario.duration_days > self.max_horizon_days:
                raise ScenarioValidationError(
                    ScenarioErrorCode.INVALID_DURATION,
                    f"duration_days must not exceed {self.max_horizon_days}",
                    field="scenario.duration_days",
                    details={
                        "duration_days": scenario.duration_days,
                        "max_horizon_days": self.max_horizon_days,
                    },
                )
            if start and end:
                span = (end - start).days + 1
                if scenario.duration_days != span:
                    raise ScenarioValidationError(
                        ScenarioErrorCode.DURATION_MISMATCH,
                        "duration_days does not match the start_date..end_date span",
                        field="scenario.duration_days",
                        details={
                            "duration_days": scenario.duration_days,
                            "window_span_days": span,
                            "start_date": start.isoformat(),
                            "end_date": end.isoformat(),
                        },
                    )

        self._validate_required_assumption_fields(scenario)
        self._validate_closure_dates(scenario, last_history_date)

    def _validate_required_assumption_fields(self, scenario: ScenarioAssumptions) -> None:
        """Every field required by the scenario type must be populated."""
        missing: List[str] = []
        for name in get_required_fields(scenario.scenario_type):
            value = getattr(scenario, name, None)
            if value is None or (isinstance(value, (list, dict)) and not value):
                missing.append(name)

        if missing:
            raise ScenarioValidationError(
                ScenarioErrorCode.MISSING_SCENARIO_FIELD,
                f"scenario_type '{scenario.scenario_type.value}' requires: "
                f"{', '.join(missing)}",
                field=f"scenario.{missing[0]}",
                details={
                    "scenario_type": scenario.scenario_type.value,
                    "missing_fields": missing,
                    "required_fields": get_required_fields(scenario.scenario_type),
                },
            )

    def _validate_closure_dates(
        self,
        scenario: ScenarioAssumptions,
        last_history_date: Optional[date],
    ) -> None:
        """Closure dates must be unique and not in the past relative to history."""
        closure_dates = list(scenario.closure_dates or [])
        if not closure_dates:
            return

        if len(set(closure_dates)) != len(closure_dates):
            duplicates = sorted({d for d in closure_dates if closure_dates.count(d) > 1})
            raise ScenarioValidationError(
                ScenarioErrorCode.DUPLICATE_CLOSURE_DATES,
                "closure_dates must not contain duplicates",
                field="scenario.closure_dates",
                details={"duplicate_dates": [d.isoformat() for d in duplicates]},
            )

        if last_history_date is None:
            return

        stale = sorted(d for d in closure_dates if d < last_history_date)
        if stale:
            raise ScenarioValidationError(
                ScenarioErrorCode.CLOSURE_DATE_IN_PAST,
                "closure_dates must not be in the past relative to the latest "
                f"history date ({last_history_date.isoformat()})",
                field="scenario.closure_dates",
                details={
                    "last_history_date": last_history_date.isoformat(),
                    "past_dates": [d.isoformat() for d in stale],
                },
            )

    def validate_confidence_levels(self, confidence_levels: Sequence[float]) -> None:
        """Levels must be non-empty, strictly within (0, 1), unique and ascending."""
        levels = list(confidence_levels or [])
        if not levels:
            raise ScenarioValidationError(
                ScenarioErrorCode.CONFIDENCE_LEVELS_EMPTY,
                "confidence_levels must not be empty",
                field="confidence_levels",
                details={"received": levels},
            )

        out_of_range = [
            level
            for level in levels
            if level is None or not math.isfinite(float(level)) or not 0.0 < float(level) < 1.0
        ]
        if out_of_range:
            raise ScenarioValidationError(
                ScenarioErrorCode.CONFIDENCE_LEVEL_OUT_OF_RANGE,
                "confidence_levels must be strictly between 0 and 1",
                field="confidence_levels",
                details={"invalid_levels": out_of_range, "received": levels},
            )

        if len(set(levels)) != len(levels):
            duplicates = sorted({level for level in levels if levels.count(level) > 1})
            raise ScenarioValidationError(
                ScenarioErrorCode.CONFIDENCE_LEVELS_NOT_UNIQUE,
                "confidence_levels must be unique",
                field="confidence_levels",
                details={"duplicate_levels": duplicates, "received": levels},
            )

        if levels != sorted(levels):
            raise ScenarioValidationError(
                ScenarioErrorCode.CONFIDENCE_LEVELS_NOT_SORTED,
                "confidence_levels must be sorted in ascending order",
                field="confidence_levels",
                details={"received": levels, "expected": sorted(levels)},
            )

    def validate_scenario_support(self, scenario_type: ScenarioType) -> None:
        """The requested scenario type must be enabled for this deployment."""
        enabled = self.enabled_scenario_types
        if scenario_type not in enabled:
            raise ScenarioValidationError(
                ScenarioErrorCode.UNSUPPORTED_SCENARIO_TYPE,
                f"scenario_type '{scenario_type.value}' is not supported",
                field="scenario.scenario_type",
                details={
                    "scenario_type": scenario_type.value,
                    "supported_scenario_types": [item.value for item in enabled],
                },
            )

    def validate_horizon(self, horizon_days: int) -> None:
        """Horizon must be positive and within the configured maximum."""
        if horizon_days is None or horizon_days < self.min_horizon_days:
            raise ScenarioValidationError(
                ScenarioErrorCode.INVALID_HORIZON,
                f"horizon_days must be at least {self.min_horizon_days}",
                field="horizon_days",
                details={
                    "horizon_days": horizon_days,
                    "min_horizon_days": self.min_horizon_days,
                },
            )
        if horizon_days > self.max_horizon_days:
            raise ScenarioValidationError(
                ScenarioErrorCode.INVALID_HORIZON,
                f"horizon_days must not exceed {self.max_horizon_days}",
                field="horizon_days",
                details={
                    "horizon_days": horizon_days,
                    "max_horizon_days": self.max_horizon_days,
                },
            )

    def validate_request(self, request: ScenarioRequest) -> None:
        """
        Run every rule over a scenario request.

        Ordered cheapest-and-most-fundamental first so the caller sees the most
        actionable error rather than a downstream symptom.
        """
        self.validate_scenario_support(request.scenario.scenario_type)
        self.validate_horizon(request.horizon_days)
        self.validate_confidence_levels(request.confidence_levels)
        self.validate_history(request.history)
        self.validate_scenario_dates(
            request.scenario,
            last_history_date=request.history[-1].date if request.history else None,
        )
        logger.debug(
            "Scenario request validated: store=%s type=%s horizon=%s",
            request.store_id,
            request.scenario.scenario_type.value,
            request.horizon_days,
        )
