"""
Inference-time feature construction.

This is the canonical implementation of the feature contract the trained models
expect. It was ported from ``ui/utils/simple_predictor.py`` -- the engineering
is the same calendar / cyclical / lag / rolling set training produced -- with two
deliberate corrections, both of which are the reason the old stack needed an
output fudge factor:

1. **Same-day columns are reconstructed from the previous day, not guessed.**
   In the M5-derived training frame ``quantity_sold == sales`` and
   ``profit == 0.3 * sales`` hold exactly, because both are summed from the same
   per-product quantity. They therefore cannot be known for a future date. The
   old stack substituted the constants 100 and 1000, which sit far outside the
   training distribution and skewed every prediction; here they are rebuilt from
   the most recent sales value so the algebraic relationship training saw still
   holds.

2. **Columns that were constant in training stay at that constant.**
   ``customer_traffic`` was zero throughout training (no source file supplied
   it). The old stack sent 500, asking the model about a region of input space
   it never saw.

``is_holiday`` is not in either category: the training pipeline overwrites the
merged column with the US public-holiday calendar
(``feature_pipeline.create_date_features``), so it is reproduced here from the
same calendar rather than carried or zeroed.

With those corrections the three Production models reproduce the history level
without any post-hoc calibration, so no calibration is applied here.

The honest consequence, recorded in every response from such a model: because
``quantity_sold`` is the target under another name, the model leans heavily on
it, and a forecast driven by a lag-1 proxy behaves largely as a persistence
forecast. That is a property of the trained model, not of this module.

Models trained without those columns instead use two calendar drivers that are
known in advance for any date, and are therefore also scenario levers:

* ``is_closed`` -- Christmas Day, when every M5 store is closed. Such a model's
  ``is_holiday`` excludes that day, exactly as training did.
* ``snap_day`` -- the store's state SNAP benefit day. M5 issues SNAP on fixed
  days of the month per state (``SNAP_DAYS_BY_STATE``, checked against the M5
  calendar by the test suite).
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from api.config import Settings, get_settings
from api.core.errors import (
    FeatureMismatchError,
    InsufficientHistoryError,
    ScenarioValidationError,
)
from api.services.artifact_service import STORE_ENCODER_KEY, ModelBundle

logger = logging.getLogger(__name__)

_LAG_PATTERN = re.compile(r"^sales_lag_(\d+)$")
_ROLLING_PATTERN = re.compile(r"^sales_rolling_(\d+)_(mean|std|min|max|median)$")

#: Calendar features derived purely from the forecast date.
_CALENDAR_FEATURES = (
    "is_holiday",
    "year",
    "month",
    "day",
    "dayofweek",
    "quarter",
    "weekofyear",
    "is_weekend",
    "month_sin",
    "month_cos",
    "day_sin",
    "day_cos",
    "dayofweek_sin",
    "dayofweek_cos",
)

#: Calendar features that also depend on the store (its state's SNAP schedule).
_STORE_CALENDAR_FEATURES = ("snap_day",)

#: Christmas Day: the one date every M5 store is closed.
CLOSED_MONTH_DAY = (12, 25)

#: Days of the month SNAP benefits are issued, per state. M5's calendar uses
#: the same days in every month of every year for each state.
SNAP_DAYS_BY_STATE: Dict[str, frozenset] = {
    "CA": frozenset(range(1, 11)),
    "TX": frozenset({1, 3, 5, 6, 7, 9, 11, 12, 13, 15}),
    "WI": frozenset({2, 3, 5, 6, 8, 9, 11, 12, 14, 15}),
}

#: Same-day measures rebuilt from the previous day's sales level.
_SALES_PROPORTIONAL_FEATURES = ("quantity_sold", "profit")

#: Features whose future value is carried from recent history, or held at the
#: value they had throughout training when history does not supply them.
_CARRIED_FEATURES = ("customer_traffic", "has_promotion")

#: Window used when carrying an exogenous feature forward from history.
CARRY_WINDOW_DAYS = 28

LEAKAGE_WARNING = (
    "This model was trained with same-day columns that are algebraically tied "
    "to the target (quantity_sold equals sales and profit is a fixed share of "
    "it). They cannot be known for a future date, so they are reconstructed "
    "from the most recent observed day. The forecast therefore stays close to "
    "the recent sales level and should be read as a level projection rather "
    "than an independent prediction of day-to-day movement."
)


@dataclass(frozen=True)
class ExogenousPolicy:
    """
    How each non-calendar, non-lag model input is produced for a future date.

    ``imputed`` names every feature the caller did not supply and this policy
    therefore had to reconstruct, so the response can declare it.
    """

    profit_to_sales_ratio: float
    quantity_to_sales_ratio: float
    carried_values: Dict[str, float] = field(default_factory=dict)
    imputed: Tuple[str, ...] = ()
    #: Two-letter state prefix of the store (``CA_1`` -> ``CA``), for SNAP.
    store_state: Optional[str] = None

    def sales_proportional(self, feature: str, sales_level: float) -> float:
        """Value of a sales-proportional feature at a given sales level."""
        if feature == "quantity_sold":
            return self.quantity_to_sales_ratio * sales_level
        if feature == "profit":
            return self.profit_to_sales_ratio * sales_level
        raise KeyError(feature)


@dataclass
class HistorySeries:
    """The caller's history, reduced to what feature building needs."""

    dates: List[date]
    sales: List[float]
    supplied_columns: Tuple[str, ...]
    column_means: Dict[str, float]
    recent_means: Dict[str, float]
    observed_ratios: Dict[str, float]
    gap_days: int = 0

    @property
    def days(self) -> int:
        """Number of observed days."""
        return len(self.sales)


class FeatureBuilder:
    """
    Builds the ordered feature matrix one forecast day at a time.

    Bound to a single model bundle, because the feature list and the store
    encoder are properties of that trained version.
    """

    def __init__(
        self, bundle: ModelBundle, settings: Optional[Settings] = None
    ) -> None:
        self.settings = settings or get_settings()
        self.bundle = bundle
        self.feature_cols: List[str] = list(bundle.feature_cols)
        self.lags, self.windows, self.unknown_features = self._classify(self.feature_cols)

    # -- feature contract --------------------------------------------------

    @staticmethod
    def _classify(
        feature_cols: Sequence[str],
    ) -> Tuple[List[int], List[Tuple[int, str]], List[str]]:
        """
        Split the feature list into the families this builder can produce.

        Anything unrecognised is returned rather than silently zero-filled: a
        feature the builder cannot derive would otherwise be sent to the model
        as a value it never saw during training.
        """
        lags: List[int] = []
        windows: List[Tuple[int, str]] = []
        unknown: List[str] = []

        known = (
            set(_CALENDAR_FEATURES)
            | {"is_closed"}
            | set(_STORE_CALENDAR_FEATURES)
            | set(_SALES_PROPORTIONAL_FEATURES)
            | set(_CARRIED_FEATURES)
            | {STORE_ENCODER_KEY}
        )

        for column in feature_cols:
            lag_match = _LAG_PATTERN.match(column)
            if lag_match:
                lags.append(int(lag_match.group(1)))
                continue
            rolling_match = _ROLLING_PATTERN.match(column)
            if rolling_match:
                windows.append((int(rolling_match.group(1)), rolling_match.group(2)))
                continue
            if column not in known:
                unknown.append(column)

        return sorted(set(lags)), sorted(set(windows)), unknown

    @property
    def uses_same_day_columns(self) -> bool:
        """
        Whether this model was trained on same-day columns tied to the target.

        Only such a model needs ``LEAKAGE_WARNING``; telling the user a model
        reads the answer back when it does not would be its own inaccuracy.
        """
        return any(column in self.feature_cols for column in _SALES_PROPORTIONAL_FEATURES)

    @property
    def required_history_days(self) -> int:
        """
        Minimum history the feature contract needs to be fully populated.

        The deepest lag or widest rolling window: below that, some inputs would
        have to be filled from a partial window.
        """
        depths = list(self.lags) + [window for window, _ in self.windows]
        return max(depths) if depths else 1

    def assert_supported(self) -> None:
        """Reject a model whose feature list this builder cannot reproduce."""
        if self.unknown_features:
            raise FeatureMismatchError(
                "The selected model expects features this service cannot "
                "build from a sales history.",
                field="model",
                details={
                    "unsupported_features": sorted(self.unknown_features)[:20],
                    "unsupported_count": len(self.unknown_features),
                },
            )

    # -- history -----------------------------------------------------------

    def read_history(self, observations: Sequence[Any]) -> HistorySeries:
        """
        Reduce the request's history into series and per-column statistics.

        Accepts anything with the ``HistoryObservation`` attributes, so the
        builder is usable from tests without constructing pydantic models.
        """
        dates = [item.date for item in observations]
        sales = [float(item.sales) for item in observations]

        optional_columns = ("quantity_sold", "profit", "customer_traffic")
        # `is_holiday` is deliberately absent: it is reproduced from the US
        # holiday calendar for every forecast day, so a supplied value could
        # only describe the past and would never reach the model.
        boolean_columns = ("has_promotion",)

        supplied: List[str] = []
        column_means: Dict[str, float] = {}
        recent_means: Dict[str, float] = {}
        observed_ratios: Dict[str, float] = {}

        recent_slice = observations[-CARRY_WINDOW_DAYS:]

        for column in optional_columns + boolean_columns:
            values = [
                float(getattr(item, column))
                for item in observations
                if getattr(item, column, None) is not None
            ]
            if not values:
                continue
            supplied.append(column)
            column_means[column] = sum(values) / len(values)
            recent = [
                float(getattr(item, column))
                for item in recent_slice
                if getattr(item, column, None) is not None
            ]
            recent_means[column] = (
                sum(recent) / len(recent) if recent else column_means[column]
            )

        # A ratio is only meaningful against the sales observed on the same
        # days, so it is computed pairwise rather than from column means.
        for column in ("quantity_sold", "profit"):
            pairs = [
                (float(getattr(item, column)), float(item.sales))
                for item in observations
                if getattr(item, column, None) is not None and item.sales > 0
            ]
            if pairs:
                sales_total = sum(sale for _, sale in pairs)
                if sales_total > 0:
                    observed_ratios[column] = sum(v for v, _ in pairs) / sales_total

        gap_days = 0
        for previous, current in zip(dates, dates[1:]):
            gap_days += max(0, (current - previous).days - 1)

        return HistorySeries(
            dates=dates,
            sales=sales,
            supplied_columns=tuple(supplied),
            column_means=column_means,
            recent_means=recent_means,
            observed_ratios=observed_ratios,
            gap_days=gap_days,
        )

    def validate_history(self, series: HistorySeries) -> List[str]:
        """
        Check the history is usable, returning non-fatal warnings.

        Raises ``InsufficientHistoryError`` when the deepest lag cannot be
        populated at all, and ``ScenarioValidationError`` for a history that is
        implausible as a sales series.
        """
        minimum = max(self.settings.min_history_days, 1)
        if series.days < minimum:
            raise InsufficientHistoryError(
                f"At least {minimum} days of history are required; "
                f"{series.days} were supplied.",
                field="history",
                details={
                    "required_days": minimum,
                    "supplied_days": series.days,
                    "model_requires_days": self.required_history_days,
                },
            )

        if series.days > self.settings.max_history_days:
            raise ScenarioValidationError(
                f"History is limited to {self.settings.max_history_days} days; "
                f"{series.days} were supplied.",
                field="history",
                details={"max_days": self.settings.max_history_days},
            )

        if not any(value > 0 for value in series.sales):
            raise ScenarioValidationError(
                "History contains no positive sales, so there is no level to "
                "forecast from.",
                field="history",
            )

        warnings: List[str] = []
        if series.days < self.required_history_days:
            warnings.append(
                f"The model uses windows up to {self.required_history_days} days, "
                f"but only {series.days} days of history were supplied; the "
                "deepest lag and rolling features were computed from a partial "
                "window."
            )
        elif series.days < self.settings.recommended_history_days:
            warnings.append(
                f"{series.days} days of history supplied; "
                f"{self.settings.recommended_history_days} or more gives the "
                "rolling features a fuller window."
            )
        if series.gap_days:
            warnings.append(
                f"History has {series.gap_days} missing calendar day(s). Lag and "
                "rolling features are positional, so gaps shift those windows."
            )
        return warnings

    # -- exogenous policy --------------------------------------------------

    def build_policy(
        self, series: HistorySeries, store_id: Optional[str] = None
    ) -> ExogenousPolicy:
        """
        Decide how to produce each non-derivable input, given this history.

        A ratio observed in the caller's own data beats the configured default,
        so a dataset with different economics still gets consistent inputs.
        ``store_id`` is needed only by store-dependent calendar features.
        """
        imputed: List[str] = []

        quantity_ratio = series.observed_ratios.get("quantity_sold")
        if quantity_ratio is None:
            quantity_ratio = 1.0
            if "quantity_sold" in self.feature_cols:
                imputed.append("quantity_sold")

        profit_ratio = series.observed_ratios.get("profit")
        if profit_ratio is None:
            profit_ratio = self.settings.profit_to_sales_ratio
            if "profit" in self.feature_cols:
                imputed.append("profit")

        carried: Dict[str, float] = {}
        for column in _CARRIED_FEATURES:
            if column not in self.feature_cols:
                continue
            if column in series.recent_means:
                carried[column] = series.recent_means[column]
            else:
                # The value this column held throughout training. Any other
                # constant would be out of distribution.
                carried[column] = 0.0
                imputed.append(column)

        store_state = str(store_id)[:2].upper() if store_id else None
        if "snap_day" in self.feature_cols and store_state not in SNAP_DAYS_BY_STATE:
            # Unknown state: no SNAP days, the value most training days held.
            imputed.append("snap_day")

        return ExogenousPolicy(
            profit_to_sales_ratio=profit_ratio,
            quantity_to_sales_ratio=quantity_ratio,
            carried_values=carried,
            imputed=tuple(imputed),
            store_state=store_state,
        )

    # -- store -------------------------------------------------------------

    def encode_store(self, store_id: str) -> float:
        """
        Encode a store identifier with the training label encoder.

        An unrecognised store is rejected rather than mapped to an arbitrary
        class: a silent substitution would return a confident forecast for the
        wrong store.
        """
        if STORE_ENCODER_KEY not in self.feature_cols:
            return 0.0

        encoder = self.bundle.encoders.get(STORE_ENCODER_KEY)
        if encoder is None:
            # No encoder artifact: fall back to the numeric suffix, which is
            # what the training data's identifiers reduce to.
            digits = "".join(char for char in str(store_id) if char.isdigit())
            return float(digits) if digits else 0.0

        known = self.bundle.supported_stores
        if store_id not in known:
            raise ScenarioValidationError(
                f"Store '{store_id}' is not one the selected model was trained on.",
                field="store_id",
                details={"supported_stores": known},
            )
        try:
            return float(encoder.transform([store_id])[0])
        except Exception as exc:  # noqa: BLE001 - reported as a request problem
            raise ScenarioValidationError(
                f"Store '{store_id}' could not be encoded for this model.",
                field="store_id",
                details={"supported_stores": known},
            ) from exc

    # -- row building ------------------------------------------------------

    @staticmethod
    def calendar_features(target_date: date) -> Dict[str, float]:
        """Calendar, cyclical, and holiday features for one date."""
        iso_week = target_date.isocalendar()[1]
        weekday = target_date.weekday()
        month = target_date.month
        day = target_date.day
        return {
            "year": float(target_date.year),
            "month": float(month),
            "day": float(day),
            "dayofweek": float(weekday),
            "quarter": float((month - 1) // 3 + 1),
            "weekofyear": float(iso_week),
            "is_weekend": 1.0 if weekday >= 5 else 0.0,
            "month_sin": math.sin(2 * math.pi * month / 12),
            "month_cos": math.cos(2 * math.pi * month / 12),
            "day_sin": math.sin(2 * math.pi * day / 31),
            "day_cos": math.cos(2 * math.pi * day / 31),
            "dayofweek_sin": math.sin(2 * math.pi * weekday / 7),
            "dayofweek_cos": math.cos(2 * math.pi * weekday / 7),
            "is_holiday": 1.0 if is_us_holiday(target_date) else 0.0,
        }

    def lag_features(self, sales_series: Sequence[float]) -> Dict[str, float]:
        """
        Lagged sales for the next day, from observed-then-predicted values.

        A lag deeper than the series falls back to the series mean, matching the
        fill training used for the first rows of each store.
        """
        values = list(sales_series)
        fallback = sum(values) / len(values) if values else 0.0
        return {
            f"sales_lag_{lag}": float(values[-lag]) if len(values) >= lag else fallback
            for lag in self.lags
        }

    def rolling_features(self, sales_series: Sequence[float]) -> Dict[str, float]:
        """Rolling statistics over the trailing window of the sales series."""
        values = [float(value) for value in sales_series]
        features: Dict[str, float] = {}
        for window, statistic in self.windows:
            sample = values[-window:] if len(values) >= window else values
            features[f"sales_rolling_{window}_{statistic}"] = _statistic(
                sample, statistic
            )
        return features

    def build_row(
        self,
        target_date: date,
        sales_series: Sequence[float],
        store_code: float,
        policy: ExogenousPolicy,
    ) -> Dict[str, float]:
        """
        Assemble one complete, correctly ordered feature row.

        ``sales_series`` is the observed history extended by every value already
        predicted in this run, which is what makes the forecast recursive.
        """
        row: Dict[str, float] = {}
        row.update(self.calendar_features(target_date))
        if "is_closed" in self.feature_cols:
            closed = (target_date.month, target_date.day) == CLOSED_MONTH_DAY
            row["is_closed"] = 1.0 if closed else 0.0
            # Training zeroes the holiday flag on a closed day; match it.
            if closed:
                row["is_holiday"] = 0.0
        if "snap_day" in self.feature_cols:
            row["snap_day"] = 1.0 if is_snap_day(policy.store_state, target_date) else 0.0
        row.update(self.lag_features(sales_series))
        row.update(self.rolling_features(sales_series))

        if STORE_ENCODER_KEY in self.feature_cols:
            row[STORE_ENCODER_KEY] = float(store_code)

        # The level the same-day measures are rebuilt from: the most recent
        # value in the series, observed or predicted.
        level = float(sales_series[-1]) if len(sales_series) else 0.0
        for feature in _SALES_PROPORTIONAL_FEATURES:
            if feature in self.feature_cols:
                row[feature] = policy.sales_proportional(feature, level)

        for feature, value in policy.carried_values.items():
            row[feature] = float(value)

        missing = [column for column in self.feature_cols if column not in row]
        if missing:
            raise FeatureMismatchError(
                "The feature row is missing inputs the model requires.",
                field="model",
                details={"missing_features": missing[:20]},
            )
        return row

    def to_frame(self, rows: Sequence[Dict[str, float]]) -> Any:
        """
        Pack feature rows into a DataFrame with the trained column order.

        A DataFrame rather than an array because both the scaler and the
        LightGBM estimator were fitted with feature names; passing a bare array
        works but makes them warn, and column order would then be an unchecked
        assumption.
        """
        import pandas as pd

        return pd.DataFrame(
            [[float(row[column]) for column in self.feature_cols] for row in rows],
            columns=self.feature_cols,
        )

    def scale(self, frame: Any) -> Any:
        """
        Apply the training feature scaler, preserving column names.

        The models were fitted on scaled features, so skipping this would send
        raw magnitudes into a model that expects standardised ones.
        """
        if self.bundle.scaler is None:
            return frame
        import pandas as pd

        scaled = self.bundle.scaler.transform(frame)
        return pd.DataFrame(scaled, columns=self.feature_cols, index=frame.index)


def _statistic(sample: Sequence[float], statistic: str) -> float:
    """One rolling statistic over a sample, with an empty sample scoring zero."""
    if not sample:
        return 0.0
    if statistic == "mean":
        return sum(sample) / len(sample)
    if statistic == "min":
        return min(sample)
    if statistic == "max":
        return max(sample)
    if statistic == "median":
        ordered = sorted(sample)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return float(ordered[middle])
        return (ordered[middle - 1] + ordered[middle]) / 2.0
    if statistic == "std":
        # Sample standard deviation (ddof=1), which is what pandas'
        # `rolling(window, min_periods=1).agg("std")` produced during training.
        # A single observation has no sample spread; pandas yields NaN there and
        # the training pipeline filled it, so zero is the equivalent value.
        if len(sample) < 2:
            return 0.0
        mean = sum(sample) / len(sample)
        variance = sum((value - mean) ** 2 for value in sample) / (len(sample) - 1)
        return math.sqrt(variance)
    raise KeyError(statistic)


def is_snap_day(state: Optional[str], target_date: date) -> bool:
    """Whether ``target_date`` is a SNAP benefit day in ``state``."""
    days = SNAP_DAYS_BY_STATE.get((state or "").upper())
    return bool(days) and target_date.day in days


_HOLIDAY_CACHE: Dict[int, Any] = {}
_HOLIDAY_UNAVAILABLE = False


def is_us_holiday(target_date: date) -> bool:
    """
    Whether a date is a US public holiday.

    Mirrors ``feature_pipeline.create_date_features``, which builds the training
    column from ``holidays.US()``. Calendars are cached per year because
    constructing one is the expensive part.

    If the ``holidays`` package is unavailable the answer degrades to False --
    the value that column held on roughly 97% of training days -- rather than
    failing the request.
    """
    global _HOLIDAY_UNAVAILABLE
    if _HOLIDAY_UNAVAILABLE:
        return False

    calendar = _HOLIDAY_CACHE.get(target_date.year)
    if calendar is None:
        try:
            import holidays

            calendar = holidays.US(years=target_date.year)
        except Exception as exc:  # noqa: BLE001 - degrade, do not fail
            logger.warning(
                "Holiday calendar unavailable; is_holiday will be 0: %s", exc
            )
            _HOLIDAY_UNAVAILABLE = True
            return False
        _HOLIDAY_CACHE[target_date.year] = calendar
    return target_date in calendar


__all__ = [
    "CARRY_WINDOW_DAYS",
    "CLOSED_MONTH_DAY",
    "LEAKAGE_WARNING",
    "SNAP_DAYS_BY_STATE",
    "ExogenousPolicy",
    "FeatureBuilder",
    "HistorySeries",
    "is_snap_day",
    "is_us_holiday",
]
