"""
Scenario impact calculation.

Computes the difference between a scenario forecast and its baseline: per-day,
cumulative, total, and -- when both sides carry quantiles -- at P10/P50/P90 so
the impact is reported as a range rather than a single number.

Pure arithmetic: no model, no database, no I/O. Every guard in here exists
because the alternative is a plausible-looking but wrong number reaching a
planner: a division by zero becoming ``inf``, a percentage computed against a
negative baseline flipping sign, or two misaligned series being subtracted
element-wise as if their dates matched.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from api.schemas.baseline_forecast import BaselineDailyForecast
from api.schemas.scenario_comparison import (
    QUANTILE_P10,
    QUANTILE_P50,
    QUANTILE_P90,
    REPORTED_QUANTILES,
    DailyImpact,
    ImpactRange,
    QuantileImpact,
    ScenarioComparisonErrorResponse,
    ScenarioImpact,
)

logger = logging.getLogger(__name__)

ALIGNMENT_ERROR_MESSAGE = "Baseline and scenario forecasts must cover identical dates."

NEGATIVE_BASELINE_NOTE = (
    "The baseline total is not positive, so percentage change is not reported."
)
ZERO_BASELINE_NOTE = (
    "Some baseline days are zero, so their percentage change is not reported."
)
NO_QUANTILE_NOTE = (
    "Quantile impact is unavailable: the model did not provide P10/P50/P90 "
    "forecasts for both the baseline and the scenario."
)

_QUANTILE_ATTRIBUTES: Dict[float, str] = {
    QUANTILE_P10: "p10",
    QUANTILE_P50: "p50",
    QUANTILE_P90: "p90",
}


class ScenarioImpactErrorCode(str, Enum):
    """Machine-readable codes for impact calculation failures."""

    FORECAST_ALIGNMENT_ERROR = "FORECAST_ALIGNMENT_ERROR"
    MISSING_FORECAST_VALUES = "MISSING_FORECAST_VALUES"
    EMPTY_FORECAST = "EMPTY_FORECAST"


class ScenarioImpactError(Exception):
    """
    An impact could not be computed from the two series.

    Mirrors the ``error_code`` / ``message`` / ``field`` / ``details`` shape used
    by the other service errors so a route layer can handle them uniformly.
    """

    def __init__(
        self,
        error_code: ScenarioImpactErrorCode | str,
        message: str,
        field_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_code: str = (
            error_code.value
            if isinstance(error_code, ScenarioImpactErrorCode)
            else error_code
        )
        self.message = message
        self.field = field_name
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

    def to_response(self) -> ScenarioComparisonErrorResponse:
        """Schema form, ready to serialise as an error body."""
        return ScenarioComparisonErrorResponse(**self.to_dict())

    def __repr__(self) -> str:
        return (
            f"ScenarioImpactError(error_code={self.error_code!r}, "
            f"field={self.field!r}, message={self.message!r})"
        )


class ScenarioImpactService:
    """Calculates scenario-vs-baseline impact."""

    def __init__(self, reported_quantiles: Sequence[float] = REPORTED_QUANTILES) -> None:
        self.reported_quantiles = tuple(reported_quantiles)

    def compute_impact(
        self,
        baseline: Sequence[BaselineDailyForecast],
        scenario: Sequence[BaselineDailyForecast],
    ) -> ScenarioImpact:
        """
        Compare two aligned forecast series.

        Raises ``ScenarioImpactError`` when the series are empty, carry missing
        or non-finite values, or do not cover identical dates.
        """
        self._validate_not_empty(baseline, scenario)
        self._validate_lengths(baseline, scenario)
        self._validate_values(baseline, "baseline")
        self._validate_values(scenario, "scenario")
        self._validate_alignment(baseline, scenario)

        notes: List[str] = []
        daily_impacts = self._build_daily_impacts(baseline, scenario, notes)

        total_baseline = sum(row.baseline_forecast for row in daily_impacts)
        total_scenario = sum(row.scenario_forecast for row in daily_impacts)
        total_difference = total_scenario - total_baseline
        total_pct = self._safe_pct(total_difference, total_baseline)
        if total_pct is None and NEGATIVE_BASELINE_NOTE not in notes:
            notes.append(NEGATIVE_BASELINE_NOTE)

        quantile_impacts, impact_range = self._quantile_impacts(baseline, scenario, notes)
        peak_date, peak_value = self._peak_impact(daily_impacts)

        return ScenarioImpact(
            horizon_days=len(daily_impacts),
            dates=[row.date for row in daily_impacts],
            daily_difference=[row.daily_difference for row in daily_impacts],
            daily_difference_pct=[row.daily_difference_pct for row in daily_impacts],
            cumulative_difference=[row.cumulative_difference for row in daily_impacts],
            total_baseline_forecast=total_baseline,
            total_scenario_forecast=total_scenario,
            total_difference=total_difference,
            total_difference_pct=total_pct,
            positive_days=sum(1 for row in daily_impacts if row.daily_difference > 0),
            negative_days=sum(1 for row in daily_impacts if row.daily_difference < 0),
            neutral_days=sum(1 for row in daily_impacts if row.daily_difference == 0),
            peak_impact_date=peak_date,
            peak_impact_value=peak_value,
            quantile_impacts=quantile_impacts,
            impact_range=impact_range,
            daily_impacts=daily_impacts,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_not_empty(
        baseline: Sequence[BaselineDailyForecast],
        scenario: Sequence[BaselineDailyForecast],
    ) -> None:
        """Neither series may be empty."""
        if not baseline or not scenario:
            raise ScenarioImpactError(
                ScenarioImpactErrorCode.EMPTY_FORECAST,
                "Both baseline and scenario forecasts must contain at least one day.",
                field_name="daily_forecasts",
                details={
                    "baseline_days": len(baseline),
                    "scenario_days": len(scenario),
                },
            )

    @staticmethod
    def _validate_lengths(
        baseline: Sequence[BaselineDailyForecast],
        scenario: Sequence[BaselineDailyForecast],
    ) -> None:
        """A length mismatch is an alignment failure, reported as such."""
        if len(baseline) != len(scenario):
            raise ScenarioImpactError(
                ScenarioImpactErrorCode.FORECAST_ALIGNMENT_ERROR,
                ALIGNMENT_ERROR_MESSAGE,
                field_name="daily_forecasts",
                details={
                    "baseline_days": len(baseline),
                    "scenario_days": len(scenario),
                    "reason": "length_mismatch",
                },
            )

    @staticmethod
    def _validate_values(
        series: Sequence[BaselineDailyForecast], label: str
    ) -> None:
        """Every day must carry a finite point forecast."""
        invalid = [
            index
            for index, row in enumerate(series)
            if row.predicted_sales is None
            or not math.isfinite(float(row.predicted_sales))
        ]
        if invalid:
            raise ScenarioImpactError(
                ScenarioImpactErrorCode.MISSING_FORECAST_VALUES,
                f"The {label} forecast contains missing or non-finite values.",
                field_name=f"{label}.predicted_sales",
                details={"indexes": invalid},
            )

    @staticmethod
    def _validate_alignment(
        baseline: Sequence[BaselineDailyForecast],
        scenario: Sequence[BaselineDailyForecast],
    ) -> None:
        """Both series must cover identical dates, in the same order."""
        baseline_dates = [row.date for row in baseline]
        scenario_dates = [row.date for row in scenario]
        if baseline_dates == scenario_dates:
            return

        only_baseline = sorted(set(baseline_dates) - set(scenario_dates))
        only_scenario = sorted(set(scenario_dates) - set(baseline_dates))
        reason = "date_mismatch" if (only_baseline or only_scenario) else "order_mismatch"
        raise ScenarioImpactError(
            ScenarioImpactErrorCode.FORECAST_ALIGNMENT_ERROR,
            ALIGNMENT_ERROR_MESSAGE,
            field_name="daily_forecasts",
            details={
                "reason": reason,
                "baseline_only_dates": [day.isoformat() for day in only_baseline],
                "scenario_only_dates": [day.isoformat() for day in only_scenario],
            },
        )

    # ------------------------------------------------------------------
    # Calculation
    # ------------------------------------------------------------------

    def _build_daily_impacts(
        self,
        baseline: Sequence[BaselineDailyForecast],
        scenario: Sequence[BaselineDailyForecast],
        notes: List[str],
    ) -> List[DailyImpact]:
        """Per-day differences, percentages, and running cumulative total."""
        rows: List[DailyImpact] = []
        cumulative = 0.0
        saw_zero_baseline = False

        for base_row, scenario_row in zip(baseline, scenario):
            base_value = float(base_row.predicted_sales)
            scenario_value = float(scenario_row.predicted_sales)
            difference = scenario_value - base_value
            cumulative += difference

            pct = self._safe_pct(difference, base_value)
            if pct is None:
                saw_zero_baseline = True

            quantile_diffs = self._daily_quantile_differences(base_row, scenario_row)
            rows.append(
                DailyImpact(
                    date=base_row.date,
                    baseline_forecast=base_value,
                    scenario_forecast=scenario_value,
                    daily_difference=difference,
                    daily_difference_pct=pct,
                    cumulative_difference=cumulative,
                    baseline_p10=base_row.p10,
                    baseline_p50=base_row.p50,
                    baseline_p90=base_row.p90,
                    scenario_p10=scenario_row.p10,
                    scenario_p50=scenario_row.p50,
                    scenario_p90=scenario_row.p90,
                    **quantile_diffs,
                )
            )

        if saw_zero_baseline and ZERO_BASELINE_NOTE not in notes:
            notes.append(ZERO_BASELINE_NOTE)
        return rows

    @staticmethod
    def _daily_quantile_differences(
        base_row: BaselineDailyForecast, scenario_row: BaselineDailyForecast
    ) -> Dict[str, Optional[float]]:
        """Per-day quantile deltas, only where both sides have the quantile."""
        differences: Dict[str, Optional[float]] = {}
        for attribute in ("p10", "p50", "p90"):
            base_value = getattr(base_row, attribute)
            scenario_value = getattr(scenario_row, attribute)
            if base_value is None or scenario_value is None:
                differences[f"difference_{attribute}"] = None
            else:
                differences[f"difference_{attribute}"] = float(scenario_value) - float(
                    base_value
                )
        return differences

    def _quantile_impacts(
        self,
        baseline: Sequence[BaselineDailyForecast],
        scenario: Sequence[BaselineDailyForecast],
        notes: List[str],
    ) -> Tuple[List[QuantileImpact], ImpactRange]:
        """
        Totals at each reported quantile.

        A quantile is only reported when *every* day of both series carries it;
        summing a partially populated quantile would understate the total.
        """
        impacts: List[QuantileImpact] = []
        range_values: Dict[str, Optional[float]] = {}

        for level in self.reported_quantiles:
            attribute = _QUANTILE_ATTRIBUTES.get(level)
            if attribute is None:
                continue

            base_values = [getattr(row, attribute) for row in baseline]
            scenario_values = [getattr(row, attribute) for row in scenario]
            if any(value is None for value in base_values) or any(
                value is None for value in scenario_values
            ):
                range_values[f"total_difference_{attribute}"] = None
                continue

            total_baseline = sum(float(value) for value in base_values)
            total_scenario = sum(float(value) for value in scenario_values)
            total_difference = total_scenario - total_baseline
            impacts.append(
                QuantileImpact(
                    quantile=level,
                    total_baseline_forecast=total_baseline,
                    total_scenario_forecast=total_scenario,
                    total_difference=total_difference,
                    total_difference_pct=self._safe_pct(total_difference, total_baseline),
                )
            )
            range_values[f"total_difference_{attribute}"] = total_difference

        impact_range = ImpactRange(**range_values)
        if not impacts and NO_QUANTILE_NOTE not in notes:
            notes.append(NO_QUANTILE_NOTE)
        return impacts, impact_range

    @staticmethod
    def _peak_impact(
        daily_impacts: Sequence[DailyImpact],
    ) -> Tuple[Optional[date], Optional[float]]:
        """
        Day of largest absolute impact, keeping its sign.

        Ties resolve to the earliest day, so the result is deterministic.
        """
        if not daily_impacts:
            return None, None
        peak = max(daily_impacts, key=lambda row: abs(row.daily_difference))
        return peak.date, peak.daily_difference

    @staticmethod
    def _safe_pct(difference: float, baseline_value: float) -> Optional[float]:
        """
        Percentage change, or None when it would be meaningless.

        A zero baseline would divide by zero; a negative baseline would invert
        the sign of the result, reporting a drop as a gain.
        """
        if baseline_value is None or not math.isfinite(float(baseline_value)):
            return None
        if float(baseline_value) <= 0:
            return None
        return (difference / float(baseline_value)) * 100.0
