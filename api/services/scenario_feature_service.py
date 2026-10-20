"""
Scenario feature override engine.

Turns planner assumptions into a modified *future* feature matrix. The baseline
frame is copied, never mutated, and only rows on or after
``forecast_start_date`` are touched.

Four invariants are enforced rather than assumed:

* history is immutable -- rows before ``forecast_start_date`` are verified
  unchanged before the result is returned;
* the target column is never written;
* lag and rolling columns are never written directly (they are derived from the
  target, so editing them would fabricate history);
* no column outside the model's feature schema is ever created.

No model is called here. This service only prepares feature matrices.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

from api.schemas.scenario import PromotionIntensity, ScenarioAssumptions, ScenarioType
from api.schemas.scenario_capabilities import FeatureSchema
from api.schemas.scenario_override import (
    AppliedOverride,
    OverrideType,
    ScenarioOverrideErrorCode,
    ScenarioOverrideErrorResponse,
    ScenarioOverrideResult,
    UnsupportedOverride,
    ValueSummary,
)

logger = logging.getLogger(__name__)

DEFAULT_TARGET_COLUMN = "sales"
DEFAULT_DATE_COLUMN = "date"
DISTINCT_SUMMARY_MAX_CARDINALITY = 10

# Columns derived from the target. Overriding them directly would fabricate
# history, so they are protected even when they appear in the feature schema.
PROTECTED_FEATURE_PATTERNS: Tuple[str, ...] = (
    r"(^|_)lag(_|$)",
    r"(^|_)rolling(_|$)",
    r"(^|_)ewm(_|$)",
    r"(^|_)velocity(_|$)",
    r"(^|_)acceleration(_|$)",
    r"_ratio_to_",
)

PROMOTION_FEATURES = ("is_promotion", "promotion_intensity", "discount_depth")
TRAFFIC_FEATURE = "customer_traffic"
CLOSURE_FEATURE = "is_closed"
HOLIDAY_FEATURE = "is_holiday"
HOLIDAY_PROXIMITY_FEATURE = "holiday_proximity"

# Numeric encoding used when `promotion_intensity` was trained as a number.
# A categorical column keeps the label instead; see `_promotion_intensity_value`.
PROMOTION_INTENSITY_FEATURE_VALUES: Dict[PromotionIntensity, float] = {
    PromotionIntensity.NONE: 0.0,
    PromotionIntensity.LOW: 0.33,
    PromotionIntensity.MEDIUM: 0.66,
    PromotionIntensity.HIGH: 1.0,
}


class ScenarioOverrideError(Exception):
    """
    A scenario could not be translated into feature overrides.

    Mirrors the ``error_code`` / ``message`` / ``field`` / ``details`` shape used
    by the other service errors so a route layer can handle them uniformly.
    """

    def __init__(
        self,
        error_code: ScenarioOverrideErrorCode | str,
        message: str,
        field_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_code: str = (
            error_code.value
            if isinstance(error_code, ScenarioOverrideErrorCode)
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

    def to_response(self) -> ScenarioOverrideErrorResponse:
        """Schema form, ready to serialise as an error body."""
        return ScenarioOverrideErrorResponse(**self.to_dict())

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(error_code={self.error_code!r}, "
            f"field={self.field!r}, message={self.message!r})"
        )


class ScenarioNotSupportedError(ScenarioOverrideError):
    """The model's feature schema cannot express this scenario at all."""

    def __init__(
        self,
        message: str,
        field_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            ScenarioOverrideErrorCode.SCENARIO_NOT_SUPPORTED,
            message,
            field_name=field_name,
            details=details,
        )


class ScenarioFeatureService:
    """
    Applies scenario assumptions to a baseline future feature matrix.

    Stateless and side-effect free: every call returns a new frame.
    """

    def __init__(
        self,
        target_column: str = DEFAULT_TARGET_COLUMN,
        date_column: str = DEFAULT_DATE_COLUMN,
        protected_patterns: Sequence[str] = PROTECTED_FEATURE_PATTERNS,
        promotion_intensity_values: Optional[Dict[PromotionIntensity, float]] = None,
    ) -> None:
        self.target_column = target_column
        self.date_column = date_column
        self._protected = [re.compile(pattern) for pattern in protected_patterns]
        self.promotion_intensity_values = (
            promotion_intensity_values or PROMOTION_INTENSITY_FEATURE_VALUES
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_scenario(
        self,
        future_features: pd.DataFrame,
        scenario: ScenarioAssumptions,
        feature_schema: FeatureSchema,
        forecast_start_date: date,
        forecast_end_date: date,
        date_column: Optional[str] = None,
    ) -> ScenarioOverrideResult:
        """
        Build the scenario feature matrix.

        Raises ``ScenarioOverrideError`` for an invalid date range or an attempt
        to touch a protected column, and ``ScenarioNotSupportedError`` when none
        of the scenario's required overrides can be applied.
        """
        date_col = date_column or self.date_column
        self._validate_range(forecast_start_date, forecast_end_date)
        self._validate_frame(future_features, date_col)

        frame = future_features.copy(deep=True)
        dates = self._normalised_dates(frame, date_col)
        future_mask = (dates >= forecast_start_date) & (dates <= forecast_end_date)

        target_dates = self._resolve_target_dates(
            scenario, forecast_start_date, forecast_end_date
        )

        applied: List[AppliedOverride] = []
        unsupported: List[UnsupportedOverride] = []

        handler = {
            ScenarioType.PROMOTION: self._apply_promotion,
            ScenarioType.TRAFFIC: self._apply_traffic,
            ScenarioType.CLOSURE: self._apply_closure,
            ScenarioType.HOLIDAY: self._apply_holiday,
            ScenarioType.CUSTOM: self._apply_custom,
        }.get(scenario.scenario_type)

        if handler is None:
            # PRICE has no override mapping yet; say so instead of silently
            # returning an unchanged baseline that looks like a real scenario.
            raise ScenarioNotSupportedError(
                f"Scenario type '{scenario.scenario_type.value}' has no feature "
                "override mapping implemented.",
                field_name="scenario.scenario_type",
                details={"scenario_type": scenario.scenario_type.value},
            )

        context = _OverrideContext(
            frame=frame,
            dates=dates,
            future_mask=future_mask,
            target_dates=target_dates,
            feature_schema=feature_schema,
            forecast_start_date=forecast_start_date,
            forecast_end_date=forecast_end_date,
        )
        handler(context, scenario, applied, unsupported)

        # Custom overrides are additive to any scenario type.
        if scenario.scenario_type is not ScenarioType.CUSTOM and scenario.custom_overrides:
            self._apply_custom(context, scenario, applied, unsupported)

        self._assert_history_unchanged(
            original=future_features,
            modified=context.frame,
            dates=dates,
            forecast_start_date=forecast_start_date,
        )

        modified_dates = sorted(
            {day for override in applied for day in override.affected_dates}
        )
        return ScenarioOverrideResult(
            scenario_future_features=context.frame,
            applied_overrides=applied,
            unsupported_overrides=unsupported,
            modified_dates=modified_dates,
        )

    # ------------------------------------------------------------------
    # Validation and guards
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_range(forecast_start_date: date, forecast_end_date: date) -> None:
        """Forecast window must be non-empty and correctly ordered."""
        if forecast_end_date < forecast_start_date:
            raise ScenarioOverrideError(
                ScenarioOverrideErrorCode.INVALID_DATE_RANGE,
                "forecast_end_date must be greater than or equal to forecast_start_date",
                field_name="forecast_end_date",
                details={
                    "forecast_start_date": forecast_start_date.isoformat(),
                    "forecast_end_date": forecast_end_date.isoformat(),
                },
            )

    def _validate_frame(self, frame: pd.DataFrame, date_col: str) -> None:
        """The feature frame must carry the date column."""
        if date_col not in frame.columns:
            raise ScenarioOverrideError(
                ScenarioOverrideErrorCode.FEATURE_NOT_AVAILABLE,
                f"Feature frame has no '{date_col}' column.",
                field_name=date_col,
                details={"columns": list(frame.columns)},
            )

    @staticmethod
    def _normalised_dates(frame: pd.DataFrame, date_col: str) -> pd.Series:
        """Date column as plain ``datetime.date`` values for comparison."""
        return pd.to_datetime(frame[date_col]).dt.date

    def is_protected(self, feature_name: str) -> bool:
        """Whether a column is derived from the target and must not be written."""
        name = feature_name.lower()
        return any(pattern.search(name) for pattern in self._protected)

    def _guard_writable(self, feature_name: str) -> None:
        """Reject a write to the target or to a derived column."""
        if feature_name == self.target_column:
            raise ScenarioOverrideError(
                ScenarioOverrideErrorCode.TARGET_COLUMN_MODIFICATION_ATTEMPTED,
                f"The target column '{feature_name}' cannot be overridden.",
                field_name=feature_name,
                details={"target_column": self.target_column},
            )
        if self.is_protected(feature_name):
            raise ScenarioOverrideError(
                ScenarioOverrideErrorCode.PROTECTED_FEATURE_MODIFICATION_ATTEMPTED,
                f"'{feature_name}' is derived from the target (lag/rolling family) "
                "and cannot be overridden directly.",
                field_name=feature_name,
                details={"feature_name": feature_name},
            )

    def _assert_history_unchanged(
        self,
        original: pd.DataFrame,
        modified: pd.DataFrame,
        dates: pd.Series,
        forecast_start_date: date,
    ) -> None:
        """Verify no pre-forecast row was touched; a violation is a bug, not input."""
        history_mask = dates < forecast_start_date
        if not history_mask.any():
            return
        before = original.loc[history_mask.values]
        after = modified.loc[history_mask.values]
        if not before.equals(after):
            changed = [
                column
                for column in before.columns
                if not before[column].equals(after[column])
            ]
            raise ScenarioOverrideError(
                ScenarioOverrideErrorCode.HISTORICAL_DATA_MODIFICATION_ATTEMPTED,
                "Scenario overrides modified rows before forecast_start_date.",
                details={
                    "forecast_start_date": forecast_start_date.isoformat(),
                    "changed_columns": changed,
                },
            )

    # ------------------------------------------------------------------
    # Date resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_target_dates(
        scenario: ScenarioAssumptions,
        forecast_start_date: date,
        forecast_end_date: date,
    ) -> List[date]:
        """
        Dates the scenario applies to, clipped to the forecast window.

        An open-ended scenario (no start/end) covers the whole horizon.
        """
        if scenario.scenario_type is ScenarioType.CLOSURE and scenario.closure_dates:
            window: Iterable[date] = scenario.closure_dates
        elif scenario.start_date and scenario.end_date:
            window = scenario.window_dates
        else:
            span = (forecast_end_date - forecast_start_date).days + 1
            window = [forecast_start_date + timedelta(days=offset) for offset in range(span)]

        return sorted(
            day for day in window if forecast_start_date <= day <= forecast_end_date
        )

    # ------------------------------------------------------------------
    # Scenario handlers
    # ------------------------------------------------------------------

    def _apply_promotion(
        self,
        context: "_OverrideContext",
        scenario: ScenarioAssumptions,
        applied: List[AppliedOverride],
        unsupported: List[UnsupportedOverride],
    ) -> None:
        """Flag a promotion window and set its intensity / discount depth."""
        self._require_dates(scenario, context)

        candidates: List[Tuple[str, Any, OverrideType]] = [
            ("is_promotion", 1, OverrideType.SET_CONSTANT)
        ]
        if "promotion_intensity" in context.schema_features:
            candidates.append(
                (
                    "promotion_intensity",
                    self._promotion_intensity_value(context, scenario.promotion_intensity),
                    OverrideType.SET_CONSTANT,
                )
            )
        if scenario.discount_depth is not None:
            candidates.append(
                ("discount_depth", scenario.discount_depth, OverrideType.SET_CONSTANT)
            )

        for feature_name, value, override_type in candidates:
            self._set_value(
                context, feature_name, value, override_type, applied, unsupported
            )

        self._require_any_applied(
            applied,
            scenario,
            unsupported,
            required=[name for name, _, _ in candidates],
        )

    def _apply_traffic(
        self,
        context: "_OverrideContext",
        scenario: ScenarioAssumptions,
        applied: List[AppliedOverride],
        unsupported: List[UnsupportedOverride],
    ) -> None:
        """Scale baseline customer traffic over the scenario window."""
        self._require_dates(scenario, context)

        if scenario.traffic_multiplier is None:
            raise ScenarioNotSupportedError(
                "A traffic scenario requires traffic_multiplier.",
                field_name="scenario.traffic_multiplier",
                details={"scenario_type": scenario.scenario_type.value},
            )

        self._multiply_value(
            context,
            TRAFFIC_FEATURE,
            scenario.traffic_multiplier,
            applied,
            unsupported,
        )
        self._require_any_applied(applied, scenario, unsupported, required=[TRAFFIC_FEATURE])

    def _apply_closure(
        self,
        context: "_OverrideContext",
        scenario: ScenarioAssumptions,
        applied: List[AppliedOverride],
        unsupported: List[UnsupportedOverride],
    ) -> None:
        """Flag closure dates. Closures in the past are rejected outright."""
        stale = [
            day for day in scenario.closure_dates if day < context.forecast_start_date
        ]
        if stale:
            raise ScenarioOverrideError(
                ScenarioOverrideErrorCode.HISTORICAL_DATA_MODIFICATION_ATTEMPTED,
                "closure_dates fall before forecast_start_date; history cannot be "
                "re-simulated.",
                field_name="scenario.closure_dates",
                details={
                    "forecast_start_date": context.forecast_start_date.isoformat(),
                    "past_dates": [day.isoformat() for day in stale],
                },
            )

        self._require_dates(scenario, context)
        self._set_value(
            context, CLOSURE_FEATURE, 1, OverrideType.SET_CONSTANT, applied, unsupported
        )
        self._require_any_applied(applied, scenario, unsupported, required=[CLOSURE_FEATURE])

    def _apply_holiday(
        self,
        context: "_OverrideContext",
        scenario: ScenarioAssumptions,
        applied: List[AppliedOverride],
        unsupported: List[UnsupportedOverride],
    ) -> None:
        """Flag holiday dates and tighten holiday proximity around them."""
        self._require_dates(scenario, context)

        self._set_value(
            context, HOLIDAY_FEATURE, 1, OverrideType.SET_CONSTANT, applied, unsupported
        )
        if HOLIDAY_PROXIMITY_FEATURE in context.schema_features:
            self._recompute_holiday_proximity(context, applied, unsupported)

        self._require_any_applied(
            applied,
            scenario,
            unsupported,
            required=[HOLIDAY_FEATURE, HOLIDAY_PROXIMITY_FEATURE],
        )

    def _apply_custom(
        self,
        context: "_OverrideContext",
        scenario: ScenarioAssumptions,
        applied: List[AppliedOverride],
        unsupported: List[UnsupportedOverride],
    ) -> None:
        """Apply caller-supplied overrides, schema-gated and guard-checked."""
        overrides = scenario.custom_overrides or {}
        if not overrides:
            if scenario.scenario_type is ScenarioType.CUSTOM:
                raise ScenarioNotSupportedError(
                    "A custom scenario requires at least one entry in custom_overrides.",
                    field_name="scenario.custom_overrides",
                    details={"scenario_type": scenario.scenario_type.value},
                )
            return

        self._require_dates(scenario, context)

        for feature_name, value in overrides.items():
            # Guards raise: a request naming the target or a derived column is a
            # client error, not a silently ignored field.
            self._guard_writable(feature_name)
            self._set_value(
                context, feature_name, value, OverrideType.CUSTOM, applied, unsupported
            )

        if scenario.scenario_type is ScenarioType.CUSTOM:
            self._require_any_applied(
                applied, scenario, unsupported, required=list(overrides.keys())
            )

    # ------------------------------------------------------------------
    # Mutation primitives
    # ------------------------------------------------------------------

    def _set_value(
        self,
        context: "_OverrideContext",
        feature_name: str,
        value: Any,
        override_type: OverrideType,
        applied: List[AppliedOverride],
        unsupported: List[UnsupportedOverride],
    ) -> None:
        """Assign a constant to the scenario rows of one feature."""
        if not self._feature_writable(context, feature_name, unsupported):
            return

        mask = context.target_mask
        column = context.frame[feature_name]
        before = self._summarise(column[mask])

        coerced = self._coerce_to_dtype(column, value)
        context.frame.loc[mask, feature_name] = coerced

        after = self._summarise(context.frame[feature_name][mask])
        applied.append(
            AppliedOverride(
                feature_name=feature_name,
                override_type=override_type,
                affected_date_count=len(context.target_dates),
                original_value_summary=before,
                new_value_summary=after,
                affected_dates=list(context.target_dates),
                detail=f"set to {coerced!r}",
            )
        )

    def _multiply_value(
        self,
        context: "_OverrideContext",
        feature_name: str,
        multiplier: float,
        applied: List[AppliedOverride],
        unsupported: List[UnsupportedOverride],
    ) -> None:
        """Scale one feature relative to its baseline value."""
        if not self._feature_writable(context, feature_name, unsupported):
            return

        mask = context.target_mask
        column = context.frame[feature_name]
        if not pd.api.types.is_numeric_dtype(column):
            unsupported.append(
                UnsupportedOverride(
                    feature_name=feature_name,
                    reason=(
                        f"'{feature_name}' is {column.dtype}, which cannot be scaled "
                        "by a multiplier."
                    ),
                    error_code=ScenarioOverrideErrorCode.FEATURE_NOT_AVAILABLE,
                )
            )
            return

        before = self._summarise(column[mask])
        scaled = column[mask].astype(float) * float(multiplier)
        if pd.api.types.is_integer_dtype(column):
            scaled = scaled.round().astype(column.dtype)
        context.frame.loc[mask, feature_name] = scaled

        after = self._summarise(context.frame[feature_name][mask])
        applied.append(
            AppliedOverride(
                feature_name=feature_name,
                override_type=OverrideType.MULTIPLY,
                affected_date_count=len(context.target_dates),
                original_value_summary=before,
                new_value_summary=after,
                affected_dates=list(context.target_dates),
                detail=f"multiplied by {multiplier}",
            )
        )

    def _recompute_holiday_proximity(
        self,
        context: "_OverrideContext",
        applied: List[AppliedOverride],
        unsupported: List[UnsupportedOverride],
    ) -> None:
        """
        Set proximity to the distance from each future day to a scenario holiday.

        The existing value is kept when it is already closer, so a real holiday
        in the baseline is never pushed further away.
        """
        feature_name = HOLIDAY_PROXIMITY_FEATURE
        if not self._feature_writable(context, feature_name, unsupported):
            return

        column = context.frame[feature_name]
        if not pd.api.types.is_numeric_dtype(column):
            unsupported.append(
                UnsupportedOverride(
                    feature_name=feature_name,
                    reason=f"'{feature_name}' is {column.dtype}, expected a numeric distance.",
                    error_code=ScenarioOverrideErrorCode.FEATURE_NOT_AVAILABLE,
                )
            )
            return

        holidays = set(context.target_dates)
        if not holidays:
            return

        future_mask = context.future_mask
        before = self._summarise(column[future_mask])

        distances = context.dates[future_mask].map(
            lambda day: min(abs((day - holiday).days) for holiday in holidays)
        )
        combined = pd.concat(
            [column[future_mask].astype(float), distances.astype(float)], axis=1
        ).min(axis=1)
        if pd.api.types.is_integer_dtype(column):
            combined = combined.round().astype(column.dtype)
        context.frame.loc[future_mask, feature_name] = combined

        after = self._summarise(context.frame[feature_name][future_mask])
        affected = sorted(context.dates[future_mask].tolist())
        applied.append(
            AppliedOverride(
                feature_name=feature_name,
                override_type=OverrideType.RECOMPUTE,
                affected_date_count=len(affected),
                original_value_summary=before,
                new_value_summary=after,
                affected_dates=affected,
                detail="recomputed as days to the nearest scenario holiday",
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _feature_writable(
        self,
        context: "_OverrideContext",
        feature_name: str,
        unsupported: List[UnsupportedOverride],
    ) -> bool:
        """
        Whether a feature can be written, recording the reason when it cannot.

        Schema membership is the gate: a column the model never saw in training
        is never created, even if the caller asks for it.
        """
        if feature_name.strip().lower() not in context.schema_features:
            unsupported.append(
                UnsupportedOverride(
                    feature_name=feature_name,
                    reason=(
                        f"'{feature_name}' is not in the model feature schema, so it "
                        "was not available during training."
                    ),
                    error_code=ScenarioOverrideErrorCode.FEATURE_NOT_AVAILABLE,
                )
            )
            return False

        if feature_name not in context.frame.columns:
            unsupported.append(
                UnsupportedOverride(
                    feature_name=feature_name,
                    reason=(
                        f"'{feature_name}' is in the feature schema but absent from the "
                        "baseline feature matrix."
                    ),
                    error_code=ScenarioOverrideErrorCode.FEATURE_NOT_AVAILABLE,
                )
            )
            return False

        if not context.target_mask.any():
            unsupported.append(
                UnsupportedOverride(
                    feature_name=feature_name,
                    reason="No forecast rows fall inside the scenario window.",
                    error_code=ScenarioOverrideErrorCode.INVALID_DATE_RANGE,
                )
            )
            return False
        return True

    def _promotion_intensity_value(
        self, context: "_OverrideContext", intensity: PromotionIntensity
    ) -> Any:
        """
        Intensity value matching how the column was trained.

        Numeric columns get the encoded level; object/categorical columns keep
        the label, so a label-encoder downstream still recognises it.
        """
        column = context.frame.get("promotion_intensity")
        if column is not None and not pd.api.types.is_numeric_dtype(column):
            return intensity.value
        return self.promotion_intensity_values[intensity]

    @staticmethod
    def _coerce_to_dtype(column: pd.Series, value: Any) -> Any:
        """Keep an integer column integral when assigning a whole number."""
        if pd.api.types.is_integer_dtype(column) and isinstance(value, (int, float)):
            if float(value).is_integer():
                return int(value)
        return value

    def _require_dates(
        self, scenario: ScenarioAssumptions, context: "_OverrideContext"
    ) -> None:
        """A scenario with no in-horizon dates cannot be simulated."""
        if not context.target_dates:
            raise ScenarioNotSupportedError(
                "The scenario window does not overlap the forecast horizon.",
                field_name="scenario.start_date",
                details={
                    "scenario_type": scenario.scenario_type.value,
                    "forecast_start_date": context.forecast_start_date.isoformat(),
                    "forecast_end_date": context.forecast_end_date.isoformat(),
                    "scenario_start_date": (
                        scenario.start_date.isoformat() if scenario.start_date else None
                    ),
                    "scenario_end_date": (
                        scenario.end_date.isoformat() if scenario.end_date else None
                    ),
                    "closure_dates": [day.isoformat() for day in scenario.closure_dates],
                },
            )

    @staticmethod
    def _require_any_applied(
        applied: List[AppliedOverride],
        scenario: ScenarioAssumptions,
        unsupported: List[UnsupportedOverride],
        required: Sequence[str],
    ) -> None:
        """Raise when every override this scenario needs turned out unsupported."""
        if applied:
            return
        raise ScenarioNotSupportedError(
            f"Scenario type '{scenario.scenario_type.value}' cannot be applied: none "
            "of its features are available in this model.",
            field_name="scenario.scenario_type",
            details={
                "scenario_type": scenario.scenario_type.value,
                "required_features": list(required),
                "unsupported": [item.model_dump(mode="json") for item in unsupported],
            },
        )

    @staticmethod
    def _summarise(values: pd.Series) -> ValueSummary:
        """Describe a column slice for the applied-override record."""
        dtype = str(values.dtype)
        count = int(values.shape[0])

        if pd.api.types.is_numeric_dtype(values) and count:
            numeric = values.astype(float)
            summary = ValueSummary(
                count=count,
                dtype=dtype,
                min=float(numeric.min()),
                max=float(numeric.max()),
                mean=float(numeric.mean()),
            )
            if numeric.nunique(dropna=True) <= DISTINCT_SUMMARY_MAX_CARDINALITY:
                summary.distinct = sorted(
                    float(item) for item in numeric.dropna().unique()
                )
            return summary

        distinct = None
        if count and values.nunique(dropna=True) <= DISTINCT_SUMMARY_MAX_CARDINALITY:
            distinct = sorted(str(item) for item in values.dropna().unique())
        return ValueSummary(count=count, dtype=dtype, distinct=distinct)


class _OverrideContext:
    """Mutable working state for one ``apply_scenario`` call."""

    def __init__(
        self,
        frame: pd.DataFrame,
        dates: pd.Series,
        future_mask: pd.Series,
        target_dates: List[date],
        feature_schema: FeatureSchema,
        forecast_start_date: date,
        forecast_end_date: date,
    ) -> None:
        self.frame = frame
        self.dates = dates
        self.future_mask = future_mask
        self.target_dates = target_dates
        self.feature_schema = feature_schema
        self.forecast_start_date = forecast_start_date
        self.forecast_end_date = forecast_end_date
        self.schema_features: Set[str] = {
            name.strip().lower() for name in feature_schema.features
        }
        self.target_mask = future_mask & dates.isin(target_dates)

    def __contains__(self, feature_name: str) -> bool:
        return feature_name in self.frame.columns
