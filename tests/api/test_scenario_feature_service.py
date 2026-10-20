"""Tests for the scenario feature override engine."""

import os
import sys
from datetime import date, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.schemas.scenario import (  # noqa: E402
    PromotionIntensity,
    ScenarioAssumptions,
    ScenarioType,
)
from api.schemas.scenario_capabilities import FeatureSchema  # noqa: E402
from api.schemas.scenario_override import (  # noqa: E402
    AppliedOverride,
    OverrideType,
    ScenarioOverrideErrorCode,
    ScenarioOverrideResult,
    ScenarioOverrideSummary,
    UnsupportedOverride,
)
from api.services.scenario_feature_service import (  # noqa: E402
    PROMOTION_INTENSITY_FEATURE_VALUES,
    ScenarioFeatureService,
    ScenarioNotSupportedError,
    ScenarioOverrideError,
)

HISTORY_START = date(2026, 1, 1)
HISTORY_DAYS = 10
FORECAST_START = HISTORY_START + timedelta(days=HISTORY_DAYS)  # 2026-01-11
HORIZON_DAYS = 14
FORECAST_END = FORECAST_START + timedelta(days=HORIZON_DAYS - 1)  # 2026-01-24

ALL_FEATURES = [
    "sales_lag_1",
    "sales_lag_7",
    "sales_rolling_7_mean",
    "is_promotion",
    "promotion_intensity",
    "discount_depth",
    "customer_traffic",
    "is_holiday",
    "holiday_proximity",
    "is_closed",
    "price",
    "dayofweek",
]


def build_frame(
    include_history: bool = True,
    columns: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Baseline feature matrix, optionally with history rows ahead of the horizon."""
    features = columns if columns is not None else ALL_FEATURES
    start = HISTORY_START if include_history else FORECAST_START
    end = FORECAST_END
    span = (end - start).days + 1
    dates = [start + timedelta(days=offset) for offset in range(span)]

    rows = []
    for index, day in enumerate(dates):
        row: Dict[str, object] = {"date": pd.Timestamp(day), "sales": 500.0 + index}
        defaults = {
            "sales_lag_1": 500.0 + index,
            "sales_lag_7": 480.0 + index,
            "sales_rolling_7_mean": 495.0 + index,
            "is_promotion": 0,
            "promotion_intensity": 0.0,
            "discount_depth": 0.0,
            "customer_traffic": 400 + index,
            "is_holiday": 0,
            "holiday_proximity": 30,
            "is_closed": 0,
            "price": 10.0,
            "dayofweek": day.weekday(),
        }
        for name in features:
            row[name] = defaults[name]
        rows.append(row)
    return pd.DataFrame(rows)


def build_schema(features: Optional[List[str]] = None) -> FeatureSchema:
    return FeatureSchema(
        schema_version="v1",
        model_version="14",
        features=features if features is not None else ALL_FEATURES,
    )


def promotion_scenario(
    start: date = FORECAST_START + timedelta(days=2),
    end: date = FORECAST_START + timedelta(days=4),
    intensity: PromotionIntensity = PromotionIntensity.HIGH,
    discount_depth: Optional[float] = 0.25,
) -> ScenarioAssumptions:
    return ScenarioAssumptions(
        scenario_type=ScenarioType.PROMOTION,
        start_date=start,
        end_date=end,
        promotion_intensity=intensity,
        discount_depth=discount_depth,
    )


@pytest.fixture
def service() -> ScenarioFeatureService:
    return ScenarioFeatureService()


def apply(
    service: ScenarioFeatureService,
    scenario: ScenarioAssumptions,
    frame: Optional[pd.DataFrame] = None,
    schema: Optional[FeatureSchema] = None,
    forecast_start: date = FORECAST_START,
    forecast_end: date = FORECAST_END,
) -> ScenarioOverrideResult:
    return service.apply_scenario(
        future_features=frame if frame is not None else build_frame(),
        scenario=scenario,
        feature_schema=schema if schema is not None else build_schema(),
        forecast_start_date=forecast_start,
        forecast_end_date=forecast_end,
    )


def rows_for(frame: pd.DataFrame, days: List[date]) -> pd.DataFrame:
    normalised = pd.to_datetime(frame["date"]).dt.date
    return frame.loc[normalised.isin(days)]


# --------------------------------------------------------------------------
# Promotion
# --------------------------------------------------------------------------


def test_promotion_affects_only_selected_future_dates(service):
    frame = build_frame()
    scenario = promotion_scenario()

    result = apply(service, scenario, frame=frame)
    out = result.scenario_future_features

    window = [
        FORECAST_START + timedelta(days=offset) for offset in (2, 3, 4)
    ]
    assert result.modified_dates == window

    assert (rows_for(out, window)["is_promotion"] == 1).all()
    outside = pd.to_datetime(out["date"]).dt.date.isin(window)
    assert (out.loc[~outside, "is_promotion"] == 0).all()


def test_promotion_sets_intensity_and_discount_depth(service):
    result = apply(service, promotion_scenario(intensity=PromotionIntensity.HIGH))
    out = result.scenario_future_features
    window = result.modified_dates

    expected = PROMOTION_INTENSITY_FEATURE_VALUES[PromotionIntensity.HIGH]
    assert (rows_for(out, window)["promotion_intensity"] == expected).all()
    assert (rows_for(out, window)["discount_depth"] == 0.25).all()

    names = result.modified_feature_names
    assert names == ["is_promotion", "promotion_intensity", "discount_depth"]


@pytest.mark.parametrize(
    "intensity",
    [
        PromotionIntensity.NONE,
        PromotionIntensity.LOW,
        PromotionIntensity.MEDIUM,
        PromotionIntensity.HIGH,
    ],
)
def test_promotion_intensity_levels_map_to_distinct_values(service, intensity):
    result = apply(service, promotion_scenario(intensity=intensity))
    out = result.scenario_future_features

    expected = PROMOTION_INTENSITY_FEATURE_VALUES[intensity]
    assert (rows_for(out, result.modified_dates)["promotion_intensity"] == expected).all()


def test_promotion_intensity_keeps_label_for_categorical_column(service):
    frame = build_frame()
    frame["promotion_intensity"] = "none"

    result = apply(service, promotion_scenario(intensity=PromotionIntensity.MEDIUM), frame=frame)
    out = result.scenario_future_features

    assert (rows_for(out, result.modified_dates)["promotion_intensity"] == "medium").all()


def test_promotion_without_discount_depth_skips_that_feature(service):
    result = apply(service, promotion_scenario(discount_depth=None))

    assert "discount_depth" not in result.modified_feature_names
    assert "is_promotion" in result.modified_feature_names


def test_promotion_applied_override_record_is_complete(service):
    result = apply(service, promotion_scenario())

    override = next(
        item for item in result.applied_overrides if item.feature_name == "is_promotion"
    )
    assert isinstance(override, AppliedOverride)
    assert override.override_type is OverrideType.SET_CONSTANT
    assert override.affected_date_count == 3
    assert override.original_value_summary.count == 3
    assert override.original_value_summary.distinct == [0.0]
    assert override.new_value_summary.distinct == [1.0]
    assert override.affected_dates == result.modified_dates


def test_promotion_falls_back_when_only_one_feature_exists(service):
    features = [name for name in ALL_FEATURES if name != "is_promotion"]
    frame = build_frame(columns=features)

    result = apply(service, promotion_scenario(), frame=frame, schema=build_schema(features))

    assert result.modified_feature_names == ["promotion_intensity", "discount_depth"]
    assert [item.feature_name for item in result.unsupported_overrides] == ["is_promotion"]


# --------------------------------------------------------------------------
# Traffic
# --------------------------------------------------------------------------


def test_traffic_multiplier_is_applied_correctly(service):
    frame = build_frame()
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.TRAFFIC,
        start_date=FORECAST_START,
        end_date=FORECAST_START + timedelta(days=2),
        traffic_multiplier=1.5,
    )
    baseline = frame.copy(deep=True)

    result = apply(service, scenario, frame=frame)
    out = result.scenario_future_features
    window = result.modified_dates

    expected = (
        rows_for(baseline, window)["customer_traffic"].astype(float) * 1.5
    ).round()
    assert list(rows_for(out, window)["customer_traffic"]) == list(expected)

    override = result.applied_overrides[0]
    assert override.override_type is OverrideType.MULTIPLY
    assert override.detail == "multiplied by 1.5"
    assert override.new_value_summary.mean > override.original_value_summary.mean


def test_traffic_multiplier_below_one_reduces_traffic(service):
    frame = build_frame()
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.TRAFFIC,
        start_date=FORECAST_START,
        end_date=FORECAST_START + timedelta(days=1),
        traffic_multiplier=0.5,
    )

    result = apply(service, scenario, frame=frame)
    override = result.applied_overrides[0]

    assert override.new_value_summary.mean < override.original_value_summary.mean


def test_traffic_leaves_rows_outside_the_window_untouched(service):
    frame = build_frame()
    baseline = frame.copy(deep=True)
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.TRAFFIC,
        start_date=FORECAST_START + timedelta(days=5),
        end_date=FORECAST_START + timedelta(days=6),
        traffic_multiplier=2.0,
    )

    out = apply(service, scenario, frame=frame).scenario_future_features

    untouched = [
        FORECAST_START + timedelta(days=offset) for offset in range(5)
    ]
    assert list(rows_for(out, untouched)["customer_traffic"]) == list(
        rows_for(baseline, untouched)["customer_traffic"]
    )


def test_traffic_without_multiplier_is_not_supported(service):
    scenario = ScenarioAssumptions.model_construct(
        scenario_type=ScenarioType.TRAFFIC,
        start_date=FORECAST_START,
        end_date=FORECAST_START + timedelta(days=1),
        traffic_multiplier=None,
        closure_dates=[],
        custom_overrides={},
        promotion_intensity=PromotionIntensity.NONE,
    )

    with pytest.raises(ScenarioNotSupportedError) as exc_info:
        apply(service, scenario)
    assert exc_info.value.error_code == "SCENARIO_NOT_SUPPORTED"


def test_non_numeric_traffic_column_is_unsupported(service):
    features = ALL_FEATURES
    frame = build_frame()
    frame["customer_traffic"] = "busy"
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.TRAFFIC,
        start_date=FORECAST_START,
        end_date=FORECAST_START + timedelta(days=1),
        traffic_multiplier=1.2,
    )

    with pytest.raises(ScenarioNotSupportedError):
        apply(service, scenario, frame=frame, schema=build_schema(features))


# --------------------------------------------------------------------------
# Closure
# --------------------------------------------------------------------------


def test_closure_dates_are_applied_correctly(service):
    closures = [FORECAST_START + timedelta(days=1), FORECAST_START + timedelta(days=5)]
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CLOSURE, closure_dates=closures
    )

    result = apply(service, scenario)
    out = result.scenario_future_features

    assert result.modified_dates == sorted(closures)
    assert (rows_for(out, closures)["is_closed"] == 1).all()

    others = pd.to_datetime(out["date"]).dt.date.isin(closures)
    assert (out.loc[~others, "is_closed"] == 0).all()


def test_closure_outside_the_horizon_is_ignored(service):
    inside = FORECAST_START + timedelta(days=1)
    outside = FORECAST_END + timedelta(days=5)
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CLOSURE, closure_dates=[inside, outside]
    )

    result = apply(service, scenario)

    assert result.modified_dates == [inside]


def test_closure_before_forecast_start_is_rejected(service):
    stale = FORECAST_START - timedelta(days=1)
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CLOSURE, closure_dates=[stale]
    )

    with pytest.raises(ScenarioOverrideError) as exc_info:
        apply(service, scenario)

    error = exc_info.value
    assert error.error_code == "HISTORICAL_DATA_MODIFICATION_ATTEMPTED"
    assert error.details["past_dates"] == [stale.isoformat()]


def test_closure_entirely_outside_horizon_is_not_supported(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CLOSURE,
        closure_dates=[FORECAST_END + timedelta(days=10)],
    )

    with pytest.raises(ScenarioNotSupportedError):
        apply(service, scenario)


def test_closure_unsupported_when_feature_absent(service):
    features = [name for name in ALL_FEATURES if name != "is_closed"]
    frame = build_frame(columns=features)
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CLOSURE,
        closure_dates=[FORECAST_START + timedelta(days=1)],
    )

    with pytest.raises(ScenarioNotSupportedError) as exc_info:
        apply(service, scenario, frame=frame, schema=build_schema(features))

    details = exc_info.value.details
    assert details["required_features"] == ["is_closed"]
    assert details["unsupported"][0]["error_code"] == "FEATURE_NOT_AVAILABLE"


# --------------------------------------------------------------------------
# Holiday
# --------------------------------------------------------------------------


def test_holiday_flags_selected_dates(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.HOLIDAY,
        start_date=FORECAST_START + timedelta(days=3),
        end_date=FORECAST_START + timedelta(days=4),
    )

    result = apply(service, scenario)
    out = result.scenario_future_features

    window = [FORECAST_START + timedelta(days=3), FORECAST_START + timedelta(days=4)]
    assert (rows_for(out, window)["is_holiday"] == 1).all()
    assert "is_holiday" in result.modified_feature_names


def test_holiday_proximity_is_recomputed(service):
    holiday = FORECAST_START + timedelta(days=5)
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.HOLIDAY, start_date=holiday, end_date=holiday
    )

    result = apply(service, scenario)
    out = result.scenario_future_features
    normalised = pd.to_datetime(out["date"]).dt.date

    proximity = dict(zip(normalised, out["holiday_proximity"]))
    assert proximity[holiday] == 0
    assert proximity[holiday - timedelta(days=1)] == 1
    assert proximity[holiday + timedelta(days=2)] == 2
    # Baseline value (30) is kept where it is already closer than the scenario.
    assert proximity[FORECAST_START] == 5

    override = next(
        item
        for item in result.applied_overrides
        if item.feature_name == "holiday_proximity"
    )
    assert override.override_type is OverrideType.RECOMPUTE


def test_holiday_proximity_never_moves_further_away(service):
    frame = build_frame()
    frame["holiday_proximity"] = 0  # baseline says every day is a holiday
    holiday = FORECAST_START + timedelta(days=5)
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.HOLIDAY, start_date=holiday, end_date=holiday
    )

    out = apply(service, scenario, frame=frame).scenario_future_features

    assert (out["holiday_proximity"] == 0).all()


def test_holiday_skips_proximity_when_not_in_schema(service):
    features = [name for name in ALL_FEATURES if name != "holiday_proximity"]
    frame = build_frame(columns=features)
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.HOLIDAY,
        start_date=FORECAST_START,
        end_date=FORECAST_START,
    )

    result = apply(service, scenario, frame=frame, schema=build_schema(features))

    assert result.modified_feature_names == ["is_holiday"]


# --------------------------------------------------------------------------
# History immutability
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario_builder",
    [
        lambda: promotion_scenario(),
        lambda: ScenarioAssumptions(
            scenario_type=ScenarioType.TRAFFIC,
            start_date=FORECAST_START,
            end_date=FORECAST_START + timedelta(days=2),
            traffic_multiplier=1.4,
        ),
        lambda: ScenarioAssumptions(
            scenario_type=ScenarioType.CLOSURE,
            closure_dates=[FORECAST_START + timedelta(days=1)],
        ),
        lambda: ScenarioAssumptions(
            scenario_type=ScenarioType.HOLIDAY,
            start_date=FORECAST_START,
            end_date=FORECAST_START + timedelta(days=1),
        ),
    ],
)
def test_historical_rows_remain_unchanged(service, scenario_builder):
    frame = build_frame()
    baseline = frame.copy(deep=True)

    out = apply(service, scenario_builder(), frame=frame).scenario_future_features

    normalised = pd.to_datetime(out["date"]).dt.date
    history = normalised < FORECAST_START
    assert history.sum() == HISTORY_DAYS
    pd.testing.assert_frame_equal(
        out.loc[history.values], baseline.loc[history.values]
    )


def test_input_frame_is_never_mutated(service):
    frame = build_frame()
    snapshot = frame.copy(deep=True)

    apply(service, promotion_scenario(), frame=frame)

    pd.testing.assert_frame_equal(frame, snapshot)


def test_works_without_history_rows(service):
    frame = build_frame(include_history=False)

    result = apply(service, promotion_scenario(), frame=frame)

    assert len(result.scenario_future_features) == HORIZON_DAYS
    assert result.modified_dates


# --------------------------------------------------------------------------
# Protected columns
# --------------------------------------------------------------------------


def test_target_column_cannot_be_modified(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CUSTOM, custom_overrides={"sales": 999.0}
    )

    with pytest.raises(ScenarioOverrideError) as exc_info:
        apply(service, scenario)

    error = exc_info.value
    assert error.error_code == "TARGET_COLUMN_MODIFICATION_ATTEMPTED"
    assert error.field == "sales"


@pytest.mark.parametrize(
    "column",
    [
        "sales_lag_1",
        "sales_lag_7",
        "sales_rolling_7_mean",
    ],
)
def test_lag_and_rolling_columns_cannot_be_overridden(service, column):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CUSTOM, custom_overrides={column: 123.0}
    )

    with pytest.raises(ScenarioOverrideError) as exc_info:
        apply(service, scenario)

    error = exc_info.value
    assert error.error_code == "PROTECTED_FEATURE_MODIFICATION_ATTEMPTED"
    assert error.field == column


@pytest.mark.parametrize(
    "column,protected",
    [
        ("sales_lag_1", True),
        ("lag_7", True),
        ("sales_rolling_7_mean", True),
        ("rolling_mean_7", True),
        ("sales_ewm_7", True),
        ("sales_velocity", True),
        ("sales_ratio_to_7d_avg", True),
        ("is_promotion", False),
        ("customer_traffic", False),
        ("price", False),
        ("flagship_store", False),
    ],
)
def test_protected_pattern_detection(service, column, protected):
    assert service.is_protected(column) is protected


def test_protected_columns_are_unchanged_by_a_promotion(service):
    frame = build_frame()
    baseline = frame.copy(deep=True)

    out = apply(service, promotion_scenario(), frame=frame).scenario_future_features

    for column in ("sales", "sales_lag_1", "sales_lag_7", "sales_rolling_7_mean"):
        pd.testing.assert_series_equal(out[column], baseline[column])


# --------------------------------------------------------------------------
# Custom overrides
# --------------------------------------------------------------------------


def test_custom_override_applies_to_schema_columns(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CUSTOM,
        start_date=FORECAST_START,
        end_date=FORECAST_START + timedelta(days=1),
        custom_overrides={"price": 8.5},
    )

    result = apply(service, scenario)
    out = result.scenario_future_features

    assert (rows_for(out, result.modified_dates)["price"] == 8.5).all()
    assert result.applied_overrides[0].override_type is OverrideType.CUSTOM


def test_custom_override_defaults_to_the_whole_horizon(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CUSTOM, custom_overrides={"price": 7.0}
    )

    result = apply(service, scenario)

    assert len(result.modified_dates) == HORIZON_DAYS
    assert result.modified_dates[0] == FORECAST_START
    assert result.modified_dates[-1] == FORECAST_END


def test_unsupported_feature_produces_a_structured_error(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CUSTOM, custom_overrides={"weather_index": 1.0}
    )

    with pytest.raises(ScenarioNotSupportedError) as exc_info:
        apply(service, scenario)

    error = exc_info.value
    assert error.error_code == "SCENARIO_NOT_SUPPORTED"
    assert error.details["required_features"] == ["weather_index"]
    unsupported = error.details["unsupported"][0]
    assert unsupported["feature_name"] == "weather_index"
    assert unsupported["error_code"] == "FEATURE_NOT_AVAILABLE"
    assert "not in the model feature schema" in unsupported["reason"]

    assert error.to_response().error_code == "SCENARIO_NOT_SUPPORTED"
    assert error.to_dict() == error.to_response().model_dump()


def test_partially_unsupported_custom_override_still_applies_the_rest(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CUSTOM,
        custom_overrides={"price": 9.0, "weather_index": 3.0},
    )

    result = apply(service, scenario)

    assert result.modified_feature_names == ["price"]
    assert [item.feature_name for item in result.unsupported_overrides] == ["weather_index"]
    assert isinstance(result.unsupported_overrides[0], UnsupportedOverride)


def test_feature_in_schema_but_absent_from_frame_is_unsupported(service):
    frame = build_frame(columns=[name for name in ALL_FEATURES if name != "price"])
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CUSTOM, custom_overrides={"price": 9.0}
    )

    with pytest.raises(ScenarioNotSupportedError) as exc_info:
        apply(service, scenario, frame=frame)

    reason = exc_info.value.details["unsupported"][0]["reason"]
    assert "absent from the baseline feature matrix" in reason


def test_custom_overrides_augment_another_scenario_type(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.PROMOTION,
        start_date=FORECAST_START,
        end_date=FORECAST_START + timedelta(days=1),
        custom_overrides={"price": 6.0},
    )

    result = apply(service, scenario)

    assert "is_promotion" in result.modified_feature_names
    assert "price" in result.modified_feature_names


def test_empty_custom_scenario_is_not_supported(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CUSTOM, custom_overrides={}
    )

    with pytest.raises(ScenarioNotSupportedError) as exc_info:
        apply(service, scenario)
    assert "at least one entry" in exc_info.value.message


def test_empty_custom_overrides_add_nothing_to_another_scenario(service):
    """An empty custom_overrides block is a no-op, not an error."""
    frame = build_frame()
    with_empty = ScenarioAssumptions(
        scenario_type=ScenarioType.PROMOTION,
        start_date=FORECAST_START,
        end_date=FORECAST_START + timedelta(days=1),
        discount_depth=None,
        custom_overrides={},
    )

    result = apply(service, with_empty, frame=frame)

    assert result.modified_feature_names == ["is_promotion", "promotion_intensity"]
    assert result.unsupported_overrides == []
    # Every column outside the promotion family is identical to the baseline.
    untouched = [
        name
        for name in frame.columns
        if name not in ("is_promotion", "promotion_intensity")
    ]
    pd.testing.assert_frame_equal(
        result.scenario_future_features[untouched], frame[untouched]
    )


def test_empty_override_produces_no_changes(service):
    """A promotion with no promotion features in schema but other columns present."""
    features = ["sales_lag_1", "customer_traffic", "dayofweek"]
    frame = build_frame(columns=features)
    baseline = frame.copy(deep=True)
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.PROMOTION,
        start_date=FORECAST_START,
        end_date=FORECAST_START + timedelta(days=1),
    )

    with pytest.raises(ScenarioNotSupportedError):
        apply(service, scenario, frame=frame, schema=build_schema(features))

    # The caller's frame is untouched even on the failure path.
    pd.testing.assert_frame_equal(frame, baseline)


# --------------------------------------------------------------------------
# Date range and result shape
# --------------------------------------------------------------------------


def test_inverted_forecast_range_is_rejected(service):
    with pytest.raises(ScenarioOverrideError) as exc_info:
        apply(
            service,
            promotion_scenario(),
            forecast_start=FORECAST_END,
            forecast_end=FORECAST_START,
        )

    assert exc_info.value.error_code == "INVALID_DATE_RANGE"
    assert exc_info.value.field == "forecast_end_date"


def test_scenario_window_outside_horizon_is_not_supported(service):
    scenario = promotion_scenario(
        start=FORECAST_END + timedelta(days=5), end=FORECAST_END + timedelta(days=6)
    )

    with pytest.raises(ScenarioNotSupportedError) as exc_info:
        apply(service, scenario)
    assert "does not overlap" in exc_info.value.message


def test_missing_date_column_is_rejected(service):
    frame = build_frame().drop(columns=["date"])

    with pytest.raises(ScenarioOverrideError) as exc_info:
        apply(service, promotion_scenario(), frame=frame)
    assert exc_info.value.error_code == "FEATURE_NOT_AVAILABLE"


def test_result_shape_and_summary(service):
    result = apply(service, promotion_scenario())

    assert isinstance(result, ScenarioOverrideResult)
    assert isinstance(result.scenario_future_features, pd.DataFrame)
    assert result.changed is True
    assert all(isinstance(item, AppliedOverride) for item in result.applied_overrides)

    summary = result.to_summary()
    assert isinstance(summary, ScenarioOverrideSummary)
    assert summary.modified_date_count == 3
    payload = summary.model_dump(mode="json")
    assert payload["applied_overrides"][0]["feature_name"] == "is_promotion"
    assert ScenarioOverrideSummary.model_validate(payload).modified_date_count == 3


def test_frame_shape_and_columns_are_preserved(service):
    frame = build_frame()

    out = apply(service, promotion_scenario(), frame=frame).scenario_future_features

    assert list(out.columns) == list(frame.columns)
    assert len(out) == len(frame)


def test_price_scenario_has_no_override_mapping_yet(service):
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.PRICE,
        start_date=FORECAST_START,
        end_date=FORECAST_START + timedelta(days=1),
    )

    with pytest.raises(ScenarioNotSupportedError) as exc_info:
        apply(service, scenario)
    assert "no feature override mapping" in exc_info.value.message


def test_error_codes_cover_the_required_set():
    required = {
        "SCENARIO_NOT_SUPPORTED",
        "FEATURE_NOT_AVAILABLE",
        "INVALID_DATE_RANGE",
        "HISTORICAL_DATA_MODIFICATION_ATTEMPTED",
        "TARGET_COLUMN_MODIFICATION_ATTEMPTED",
    }
    assert required <= {code.value for code in ScenarioOverrideErrorCode}
