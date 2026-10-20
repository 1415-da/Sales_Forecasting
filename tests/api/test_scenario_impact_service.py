"""Tests for scenario impact calculation and comparison orchestration."""

import os
import sys
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.schemas.baseline_forecast import (  # noqa: E402
    BaselineDailyForecast,
    BaselineForecastResponse,
    BaselineForecastSummary,
    ModelStage,
    UncertaintyMethod,
)
from api.schemas.scenario import (  # noqa: E402
    HistoryPoint,
    PromotionIntensity,
    ScenarioAssumptions,
    ScenarioRequest,
    ScenarioType,
)
from api.schemas.scenario_capabilities import FeatureSchema  # noqa: E402
from api.schemas.scenario_comparison import (  # noqa: E402
    SCENARIO_LIMITATIONS,
    DailyImpact,
    ForecastType,
    ImpactRange,
    QuantileImpact,
    ScenarioComparisonResponse,
    ScenarioImpact,
)
from api.services.baseline_forecast_service import ResolvedModel  # noqa: E402
from api.services.scenario_forecast_service import (  # noqa: E402
    BASELINE_RECOMPUTED_WARNING,
    UNSUPPORTED_OVERRIDE_WARNING,
    MatrixPrediction,
    ScenarioComparisonError,
    ScenarioForecastService,
)
from api.services.scenario_impact_service import (  # noqa: E402
    ALIGNMENT_ERROR_MESSAGE,
    NEGATIVE_BASELINE_NOTE,
    NO_QUANTILE_NOTE,
    ZERO_BASELINE_NOTE,
    ScenarioImpactError,
    ScenarioImpactErrorCode,
    ScenarioImpactService,
)

FORECAST_START = date(2026, 2, 1)
HORIZON_DAYS = 7
FORECAST_END = FORECAST_START + timedelta(days=HORIZON_DAYS - 1)
HISTORY_START = date(2026, 1, 1)
HISTORY_DAYS = 31


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def daily(
    values: Sequence[float],
    start: date = FORECAST_START,
    quantiles: Optional[Dict[str, Sequence[float]]] = None,
) -> List[BaselineDailyForecast]:
    """Build a daily forecast series, optionally with quantiles."""
    rows = []
    for index, value in enumerate(values):
        bounds = sorted((float(value) * 0.9, float(value) * 1.1))
        row = {
            "date": start + timedelta(days=index),
            "predicted_sales": float(value),
            "lower_bound": bounds[0],
            "upper_bound": bounds[1],
        }
        if quantiles:
            row["p10"] = float(quantiles["p10"][index])
            row["p50"] = float(quantiles["p50"][index])
            row["p90"] = float(quantiles["p90"][index])
            row["lower_bound"] = row["p10"]
            row["upper_bound"] = row["p90"]
            row["uncertainty_method"] = UncertaintyMethod.QUANTILE_MODEL
        rows.append(BaselineDailyForecast(**row))
    return rows


@pytest.fixture
def impact_service() -> ScenarioImpactService:
    return ScenarioImpactService()


# --------------------------------------------------------------------------
# Promotion scenario with positive uplift
# --------------------------------------------------------------------------


def test_promotion_uplift_produces_positive_impact(impact_service):
    baseline = daily([100.0] * 5)
    scenario = daily([110.0, 120.0, 130.0, 100.0, 100.0])

    impact = impact_service.compute_impact(baseline, scenario)

    assert isinstance(impact, ScenarioImpact)
    assert impact.horizon_days == 5
    assert impact.daily_difference == [10.0, 20.0, 30.0, 0.0, 0.0]
    assert impact.daily_difference_pct == [10.0, 20.0, 30.0, 0.0, 0.0]
    assert impact.cumulative_difference == [10.0, 30.0, 60.0, 60.0, 60.0]
    assert impact.total_baseline_forecast == 500.0
    assert impact.total_scenario_forecast == 560.0
    assert impact.total_difference == 60.0
    assert impact.total_difference_pct == pytest.approx(12.0)
    assert impact.positive_days == 3
    assert impact.negative_days == 0
    assert impact.neutral_days == 2
    assert impact.direction == "increase"


def test_daily_impacts_are_returned_per_day(impact_service):
    baseline = daily([200.0, 200.0])
    scenario = daily([220.0, 180.0])

    impact = impact_service.compute_impact(baseline, scenario)

    assert len(impact.daily_impacts) == 2
    first, second = impact.daily_impacts
    assert isinstance(first, DailyImpact)
    assert first.date == FORECAST_START
    assert first.baseline_forecast == 200.0
    assert first.scenario_forecast == 220.0
    assert first.daily_difference == 20.0
    assert first.daily_difference_pct == pytest.approx(10.0)
    assert first.cumulative_difference == 20.0
    assert second.cumulative_difference == 0.0


# --------------------------------------------------------------------------
# Closure scenario with negative impact
# --------------------------------------------------------------------------


def test_closure_produces_negative_impact(impact_service):
    baseline = daily([100.0] * 5)
    scenario = daily([100.0, 0.0, 0.0, 100.0, 100.0])  # closed for two days

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.daily_difference == [0.0, -100.0, -100.0, 0.0, 0.0]
    assert impact.total_difference == -200.0
    assert impact.total_difference_pct == pytest.approx(-40.0)
    assert impact.negative_days == 2
    assert impact.positive_days == 0
    assert impact.direction == "decrease"
    assert impact.cumulative_difference[-1] == -200.0


def test_full_closure_caps_at_minus_one_hundred_percent(impact_service):
    baseline = daily([50.0] * 3)
    scenario = daily([0.0] * 3)

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.total_difference_pct == pytest.approx(-100.0)
    assert impact.daily_difference_pct == [-100.0, -100.0, -100.0]


# --------------------------------------------------------------------------
# Traffic reduction
# --------------------------------------------------------------------------


def test_traffic_reduction_scales_impact_down(impact_service):
    baseline = daily([400.0, 500.0, 600.0])
    scenario = daily([320.0, 400.0, 480.0])  # 20% traffic-driven reduction

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.daily_difference == [-80.0, -100.0, -120.0]
    assert all(pct == pytest.approx(-20.0) for pct in impact.daily_difference_pct)
    assert impact.total_difference == -300.0
    assert impact.total_difference_pct == pytest.approx(-20.0)
    assert impact.negative_days == 3


# --------------------------------------------------------------------------
# Zero and negative baseline protection
# --------------------------------------------------------------------------


def test_zero_baseline_day_yields_no_percentage(impact_service):
    baseline = daily([0.0, 100.0])
    scenario = daily([50.0, 110.0])

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.daily_difference == [50.0, 10.0]
    assert impact.daily_difference_pct[0] is None  # not inf
    assert impact.daily_difference_pct[1] == pytest.approx(10.0)
    assert ZERO_BASELINE_NOTE in impact.notes


def test_zero_baseline_total_yields_no_total_percentage(impact_service):
    baseline = daily([0.0, 0.0])
    scenario = daily([25.0, 30.0])

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.total_baseline_forecast == 0.0
    assert impact.total_difference == 55.0
    assert impact.total_difference_pct is None
    assert NEGATIVE_BASELINE_NOTE in impact.notes


def test_negative_baseline_total_yields_no_percentage(impact_service):
    baseline = daily([-100.0, 50.0])
    scenario = daily([-50.0, 60.0])

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.total_baseline_forecast == -50.0
    # A negative denominator would invert the sign and report a gain as a loss.
    assert impact.total_difference_pct is None
    assert impact.total_difference == 60.0


def test_no_division_by_zero_anywhere(impact_service):
    baseline = daily([0.0] * 4)
    scenario = daily([0.0] * 4)

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.total_difference == 0.0
    assert impact.total_difference_pct is None
    assert all(pct is None for pct in impact.daily_difference_pct)
    assert impact.neutral_days == 4
    assert impact.direction == "neutral"


# --------------------------------------------------------------------------
# Alignment guards
# --------------------------------------------------------------------------


def test_mismatched_forecast_lengths_raise_alignment_error(impact_service):
    baseline = daily([100.0] * 5)
    scenario = daily([100.0] * 3)

    with pytest.raises(ScenarioImpactError) as exc_info:
        impact_service.compute_impact(baseline, scenario)

    error = exc_info.value
    assert error.error_code == "FORECAST_ALIGNMENT_ERROR"
    assert error.message == ALIGNMENT_ERROR_MESSAGE
    assert error.message == "Baseline and scenario forecasts must cover identical dates."
    assert error.details["baseline_days"] == 5
    assert error.details["scenario_days"] == 3
    assert error.details["reason"] == "length_mismatch"


def test_mismatched_dates_raise_alignment_error(impact_service):
    baseline = daily([100.0] * 3, start=FORECAST_START)
    scenario = daily([100.0] * 3, start=FORECAST_START + timedelta(days=1))

    with pytest.raises(ScenarioImpactError) as exc_info:
        impact_service.compute_impact(baseline, scenario)

    error = exc_info.value
    assert error.error_code == "FORECAST_ALIGNMENT_ERROR"
    assert error.message == ALIGNMENT_ERROR_MESSAGE
    assert error.details["reason"] == "date_mismatch"
    assert FORECAST_START.isoformat() in error.details["baseline_only_dates"]


def test_reordered_dates_raise_alignment_error(impact_service):
    baseline = daily([100.0, 200.0, 300.0])
    scenario = list(reversed(daily([100.0, 200.0, 300.0])))

    with pytest.raises(ScenarioImpactError) as exc_info:
        impact_service.compute_impact(baseline, scenario)

    assert exc_info.value.details["reason"] == "order_mismatch"


def test_empty_series_raise_empty_forecast(impact_service):
    with pytest.raises(ScenarioImpactError) as exc_info:
        impact_service.compute_impact([], daily([100.0]))
    assert exc_info.value.error_code == "EMPTY_FORECAST"


def test_missing_forecast_values_are_rejected(impact_service):
    baseline = daily([100.0, 100.0])
    broken = daily([100.0, 100.0])
    broken[1] = BaselineDailyForecast.model_construct(
        date=broken[1].date,
        predicted_sales=float("nan"),
        lower_bound=0.0,
        upper_bound=0.0,
    )

    with pytest.raises(ScenarioImpactError) as exc_info:
        impact_service.compute_impact(baseline, broken)

    error = exc_info.value
    assert error.error_code == "MISSING_FORECAST_VALUES"
    assert error.details["indexes"] == [1]


def test_none_forecast_value_is_rejected(impact_service):
    broken = [
        BaselineDailyForecast.model_construct(
            date=FORECAST_START, predicted_sales=None, lower_bound=0.0, upper_bound=0.0
        )
    ]

    with pytest.raises(ScenarioImpactError) as exc_info:
        impact_service.compute_impact(broken, daily([100.0]))
    assert exc_info.value.error_code == "MISSING_FORECAST_VALUES"


def test_impact_error_serialises():
    error = ScenarioImpactError(
        ScenarioImpactErrorCode.FORECAST_ALIGNMENT_ERROR,
        ALIGNMENT_ERROR_MESSAGE,
        field_name="daily_forecasts",
        details={"reason": "length_mismatch"},
    )

    response = error.to_response()
    assert response.error_code == "FORECAST_ALIGNMENT_ERROR"
    assert response.message == ALIGNMENT_ERROR_MESSAGE
    assert error.to_dict() == response.model_dump()


# --------------------------------------------------------------------------
# Quantile impact
# --------------------------------------------------------------------------


def test_quantile_impact_is_calculated_at_p10_p50_p90(impact_service):
    baseline = daily(
        [100.0, 100.0],
        quantiles={"p10": [80.0, 80.0], "p50": [100.0, 100.0], "p90": [120.0, 120.0]},
    )
    scenario = daily(
        [130.0, 130.0],
        quantiles={"p10": [100.0, 100.0], "p50": [130.0, 130.0], "p90": [160.0, 160.0]},
    )

    impact = impact_service.compute_impact(baseline, scenario)

    assert [item.quantile for item in impact.quantile_impacts] == [0.1, 0.5, 0.9]
    by_level = {item.quantile: item for item in impact.quantile_impacts}

    p10 = by_level[0.1]
    assert isinstance(p10, QuantileImpact)
    assert p10.total_baseline_forecast == 160.0
    assert p10.total_scenario_forecast == 200.0
    assert p10.total_difference == 40.0
    assert p10.total_difference_pct == pytest.approx(25.0)

    assert by_level[0.5].total_difference == 60.0
    assert by_level[0.9].total_difference == 80.0
    assert NO_QUANTILE_NOTE not in impact.notes


def test_quantile_impact_returns_a_range_not_just_a_point(impact_service):
    baseline = daily(
        [100.0],
        quantiles={"p10": [80.0], "p50": [100.0], "p90": [120.0]},
    )
    scenario = daily(
        [130.0],
        quantiles={"p10": [95.0], "p50": [130.0], "p90": [175.0]},
    )

    impact = impact_service.compute_impact(baseline, scenario)
    impact_range = impact.impact_range

    assert isinstance(impact_range, ImpactRange)
    assert impact_range.total_difference_p10 == 15.0
    assert impact_range.total_difference_p50 == 30.0
    assert impact_range.total_difference_p90 == 55.0
    assert impact_range.is_available is True
    assert impact_range.spread == 40.0
    # The point impact alone would hide that the plausible range is 15..55.
    assert impact.total_difference == 30.0


def test_daily_quantile_differences_are_reported(impact_service):
    baseline = daily(
        [100.0], quantiles={"p10": [80.0], "p50": [100.0], "p90": [120.0]}
    )
    scenario = daily(
        [110.0], quantiles={"p10": [88.0], "p50": [110.0], "p90": [132.0]}
    )

    row = impact_service.compute_impact(baseline, scenario).daily_impacts[0]

    assert row.baseline_p10 == 80.0
    assert row.scenario_p90 == 132.0
    assert row.difference_p10 == pytest.approx(8.0)
    assert row.difference_p50 == pytest.approx(10.0)
    assert row.difference_p90 == pytest.approx(12.0)


def test_missing_quantiles_produce_no_quantile_impact(impact_service):
    impact = impact_service.compute_impact(daily([100.0]), daily([120.0]))

    assert impact.quantile_impacts == []
    assert impact.impact_range.is_available is False
    assert impact.impact_range.spread is None
    assert NO_QUANTILE_NOTE in impact.notes


def test_partial_quantiles_are_not_summed(impact_service):
    """A quantile present on only some days would understate the total."""
    baseline = daily(
        [100.0, 100.0],
        quantiles={"p10": [80.0, 80.0], "p50": [100.0, 100.0], "p90": [120.0, 120.0]},
    )
    scenario = daily(
        [110.0, 110.0],
        quantiles={"p10": [88.0, 88.0], "p50": [110.0, 110.0], "p90": [132.0, 132.0]},
    )
    scenario[1] = scenario[1].model_copy(update={"p10": None})

    impact = impact_service.compute_impact(baseline, scenario)

    levels = [item.quantile for item in impact.quantile_impacts]
    assert 0.1 not in levels
    assert levels == [0.5, 0.9]
    assert impact.impact_range.total_difference_p10 is None
    assert impact.impact_range.total_difference_p50 == 20.0


# --------------------------------------------------------------------------
# Peak impact detection
# --------------------------------------------------------------------------


def test_peak_impact_detects_largest_positive_day(impact_service):
    baseline = daily([100.0] * 4)
    scenario = daily([105.0, 150.0, 110.0, 100.0])

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.peak_impact_date == FORECAST_START + timedelta(days=1)
    assert impact.peak_impact_value == 50.0


def test_peak_impact_detects_largest_negative_day_and_keeps_sign(impact_service):
    baseline = daily([100.0] * 4)
    scenario = daily([110.0, 95.0, 0.0, 105.0])

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.peak_impact_date == FORECAST_START + timedelta(days=2)
    assert impact.peak_impact_value == -100.0


def test_peak_impact_ties_resolve_to_the_earliest_day(impact_service):
    baseline = daily([100.0] * 3)
    scenario = daily([120.0, 120.0, 100.0])

    impact = impact_service.compute_impact(baseline, scenario)

    assert impact.peak_impact_date == FORECAST_START
    assert impact.peak_impact_value == 20.0


def test_peak_impact_is_none_when_nothing_changes(impact_service):
    impact = impact_service.compute_impact(daily([100.0] * 3), daily([100.0] * 3))

    assert impact.peak_impact_value == 0.0
    assert impact.peak_impact_date == FORECAST_START
    assert impact.total_difference == 0.0


# --------------------------------------------------------------------------
# Comparison orchestration (ScenarioForecastService)
# --------------------------------------------------------------------------


ALL_FEATURES = [
    "sales_lag_1",
    "sales_rolling_7_mean",
    "is_promotion",
    "promotion_intensity",
    "discount_depth",
    "customer_traffic",
    "is_closed",
    "is_holiday",
]


class FakeFeatureBuilder:
    """Builds a deterministic future matrix for the horizon."""

    def __init__(self, features: Optional[List[str]] = None) -> None:
        self.features = features if features is not None else ALL_FEATURES
        self.calls = 0

    def build(
        self,
        *,
        history,
        store_id,
        forecast_start_date,
        forecast_end_date,
        feature_schema,
        model,
    ) -> pd.DataFrame:
        self.calls += 1
        span = (forecast_end_date - forecast_start_date).days + 1
        rows = []
        for offset in range(span):
            row = {"date": pd.Timestamp(forecast_start_date + timedelta(days=offset))}
            defaults = {
                "sales_lag_1": 500.0,
                "sales_rolling_7_mean": 500.0,
                "is_promotion": 0,
                "promotion_intensity": 0.0,
                "discount_depth": 0.0,
                "customer_traffic": 400,
                "is_closed": 0,
                "is_holiday": 0,
            }
            for name in self.features:
                row[name] = defaults[name]
            rows.append(row)
        return pd.DataFrame(rows)


class FakeMatrixPredictor:
    """
    Predicts 100 per row, plus 50 when promoted and minus 100 when closed.

    Deliberately simple so the comparison arithmetic is checkable by hand.
    """

    def __init__(self, with_quantiles: bool = False, error: Optional[Exception] = None):
        self.with_quantiles = with_quantiles
        self.error = error
        self.calls = 0

    def predict_matrix(self, *, features, model, confidence_levels) -> MatrixPrediction:
        self.calls += 1
        if self.error:
            raise self.error

        dates = list(pd.to_datetime(features["date"]).dt.date)
        values = []
        for _, row in features.iterrows():
            value = 100.0
            if row.get("is_promotion", 0):
                value += 50.0
            if row.get("is_closed", 0):
                value -= 100.0
            traffic = float(row.get("customer_traffic", 400))
            value *= traffic / 400.0
            values.append(value)

        quantiles: Dict[float, List[float]] = {}
        method = UncertaintyMethod.RESIDUAL_BASED
        if self.with_quantiles:
            quantiles = {
                0.1: [value * 0.8 for value in values],
                0.5: list(values),
                0.9: [value * 1.2 for value in values],
            }
            method = UncertaintyMethod.QUANTILE_MODEL
        return MatrixPrediction(
            dates=dates, predictions=values, quantiles=quantiles, uncertainty_method=method
        )


class FakeBaselineService:
    """Returns a canned baseline response."""

    def __init__(self) -> None:
        self.calls = 0

    def create_baseline(self, request: ScenarioRequest) -> BaselineForecastResponse:
        self.calls += 1
        rows = daily([100.0] * request.horizon_days)
        return BaselineForecastResponse(
            baseline_forecast_id="bf_canned",
            store_id=request.store_id,
            model_version="14",
            model_stage=ModelStage.PRODUCTION.value,
            horizon_days=request.horizon_days,
            history_start_date=request.history[0].date,
            history_end_date=request.history[-1].date,
            forecast_start_date=FORECAST_START,
            forecast_end_date=FORECAST_START + timedelta(days=request.horizon_days - 1),
            daily_forecasts=rows,
            summary=BaselineForecastSummary.from_daily(rows),
            feature_schema_version="v1",
            data_source="kaggle_m5",
            dataset_version="v1.0.0",
            run_id="a" * 32,
        )


class FakeSchemaService:
    """Returns a canned feature schema."""

    def __init__(self, features: Optional[List[str]] = None) -> None:
        self.features = features if features is not None else ALL_FEATURES

    def get_feature_schema(self, model_version: str) -> FeatureSchema:
        return FeatureSchema(
            schema_version="v1", model_version="14", features=self.features
        )


class FakeBaselineProvider:
    """Resolves a reusable baseline, or raises."""

    def __init__(self, resolved=None, error: Optional[Exception] = None) -> None:
        self.resolved = resolved
        self.error = error

    def resolve(self, baseline_forecast_id, request):
        if self.error:
            raise self.error
        return self.resolved


def build_request(
    horizon_days: int = HORIZON_DAYS,
    scenario: Optional[ScenarioAssumptions] = None,
    **overrides,
) -> ScenarioRequest:
    payload = {
        "store_id": "store_001",
        "horizon_days": horizon_days,
        "history": [
            HistoryPoint(
                date=HISTORY_START + timedelta(days=offset),
                sales=500.0,
                store_id="store_001",
            )
            for offset in range(HISTORY_DAYS)
        ],
        "scenario": scenario
        or ScenarioAssumptions(
            scenario_type=ScenarioType.PROMOTION,
            start_date=FORECAST_START,
            end_date=FORECAST_START + timedelta(days=2),
            promotion_intensity=PromotionIntensity.HIGH,
        ),
    }
    payload.update(overrides)
    return ScenarioRequest(**payload)


def build_forecast_service(
    features: Optional[List[str]] = None,
    with_quantiles: bool = False,
    predictor: Optional[FakeMatrixPredictor] = None,
    baseline_provider: Optional[FakeBaselineProvider] = None,
    baseline_service: Optional[FakeBaselineService] = None,
) -> ScenarioForecastService:
    return ScenarioForecastService(
        feature_builder=FakeFeatureBuilder(features),
        matrix_predictor=predictor or FakeMatrixPredictor(with_quantiles=with_quantiles),
        baseline_service=baseline_service or FakeBaselineService(),
        feature_schema_service=FakeSchemaService(features),
        baseline_provider=baseline_provider,
    )


def test_comparison_response_is_complete():
    service = build_forecast_service()

    response = service.create_comparison(build_request())

    assert isinstance(response, ScenarioComparisonResponse)
    assert response.scenario_id.startswith("scn_")
    assert response.baseline_forecast_id == "bf_canned"
    assert response.scenario_forecast_id.startswith("sf_")
    assert response.model_version == "14"
    assert response.model_stage == "Production"
    assert response.scenario_type is ScenarioType.PROMOTION
    assert response.forecast_type is ForecastType.PREDICTIVE_WHAT_IF
    assert response.baseline.horizon_days == HORIZON_DAYS
    assert response.scenario.horizon_days == HORIZON_DAYS
    assert response.impact.horizon_days == HORIZON_DAYS
    assert len(response.daily_forecasts) == HORIZON_DAYS
    assert response.applied_overrides
    assert response.unsupported_overrides == []
    assert "is_promotion" in response.modified_features
    assert response.limitations
    assert response.generated_at is not None


def test_forecast_type_is_always_predictive_what_if():
    service = build_forecast_service()

    response = service.create_comparison(build_request())

    assert response.forecast_type.value == "predictive_what_if"
    assert response.model_dump(mode="json")["forecast_type"] == "predictive_what_if"


def test_limitations_are_attached_verbatim_to_every_response():
    service = build_forecast_service()

    response = service.create_comparison(build_request())

    assert response.limitations == list(SCENARIO_LIMITATIONS)
    assert response.limitations == [
        "This is a predictive what-if estimate based on the selected production model.",
        "It does not establish causal impact.",
        "Assumes scenario effects resemble historical patterns.",
        "Uncertainty reflects model error and does not include external shocks.",
    ]


def test_promotion_comparison_shows_uplift_on_window_days_only():
    service = build_forecast_service()

    response = service.create_comparison(build_request())
    impact = response.impact

    # The fake model adds 50 per promoted day; the window is 3 days.
    assert impact.total_difference == pytest.approx(150.0)
    assert impact.positive_days == 3
    assert impact.neutral_days == HORIZON_DAYS - 3
    assert impact.daily_difference[:3] == [50.0, 50.0, 50.0]
    assert impact.daily_difference[3:] == [0.0] * (HORIZON_DAYS - 3)
    assert impact.total_difference_pct == pytest.approx(
        150.0 / (100.0 * HORIZON_DAYS) * 100.0
    )


def test_closure_comparison_shows_negative_impact():
    closures = [FORECAST_START + timedelta(days=1), FORECAST_START + timedelta(days=2)]
    service = build_forecast_service()

    response = service.create_comparison(
        build_request(
            scenario=ScenarioAssumptions(
                scenario_type=ScenarioType.CLOSURE, closure_dates=closures
            )
        )
    )

    assert response.impact.total_difference == pytest.approx(-200.0)
    assert response.impact.negative_days == 2
    assert response.impact.direction == "decrease"
    assert response.modified_dates == closures


def test_traffic_reduction_comparison():
    service = build_forecast_service()

    response = service.create_comparison(
        build_request(
            scenario=ScenarioAssumptions(
                scenario_type=ScenarioType.TRAFFIC,
                start_date=FORECAST_START,
                end_date=FORECAST_START + timedelta(days=1),
                traffic_multiplier=0.5,
            )
        )
    )

    # Traffic halves on two days, and the fake model scales linearly with it.
    assert response.impact.daily_difference[0] == pytest.approx(-50.0)
    assert response.impact.daily_difference[1] == pytest.approx(-50.0)
    assert response.impact.total_difference == pytest.approx(-100.0)
    assert response.impact.negative_days == 2


def test_both_sides_are_predicted_from_feature_matrices():
    predictor = FakeMatrixPredictor()
    service = build_forecast_service(predictor=predictor)

    service.create_comparison(build_request())

    # One call for the baseline matrix, one for the scenario matrix.
    assert predictor.calls == 2


def test_quantile_comparison_populates_impact_range():
    service = build_forecast_service(with_quantiles=True)

    response = service.create_comparison(build_request())

    assert response.baseline.has_quantiles is True
    assert response.scenario.has_quantiles is True
    assert response.impact.impact_range.is_available is True
    assert len(response.impact.quantile_impacts) == 3
    assert response.daily_forecasts[0].difference_p50 == pytest.approx(50.0)
    assert (
        response.baseline.uncertainty_method is UncertaintyMethod.QUANTILE_MODEL
    )


def test_unsupported_override_is_reported_with_a_warning():
    """A model without `is_promotion` yields a partial, clearly-flagged scenario."""
    features = [name for name in ALL_FEATURES if name != "is_promotion"]
    service = build_forecast_service(features=features)

    response = service.create_comparison(build_request())

    assert [item.feature_name for item in response.unsupported_overrides] == [
        "is_promotion"
    ]
    assert UNSUPPORTED_OVERRIDE_WARNING in response.warnings
    # promotion_intensity still applied, but this model ignores it, so the
    # honest answer is a zero impact on a partial scenario -- not a fake uplift.
    assert "promotion_intensity" in response.modified_features
    assert response.impact.total_difference == pytest.approx(0.0)


def test_new_baseline_is_generated_when_no_id_is_supplied():
    baseline_service = FakeBaselineService()
    service = build_forecast_service(baseline_service=baseline_service)

    response = service.create_comparison(build_request())

    assert baseline_service.calls == 1
    assert response.baseline_reused is False
    assert BASELINE_RECOMPUTED_WARNING not in response.warnings


def test_supplied_baseline_is_reused_and_recomputation_is_disclosed():
    from api.services.scenario_forecast_service import ResolvedBaseline

    resolved = ResolvedBaseline(
        baseline_forecast_id="bf_existing",
        model_version="14",
        model_stage=ModelStage.PRODUCTION.value,
        forecast_start_date=FORECAST_START,
        forecast_end_date=FORECAST_END,
        horizon_days=HORIZON_DAYS,
        run_id="a" * 32,
        reused=True,
    )
    baseline_service = FakeBaselineService()
    service = build_forecast_service(
        baseline_provider=FakeBaselineProvider(resolved=resolved),
        baseline_service=baseline_service,
    )

    response = service.create_comparison(
        build_request(baseline_forecast_id="bf_existing")
    )

    assert response.baseline_forecast_id == "bf_existing"
    assert response.baseline_reused is True
    assert baseline_service.calls == 0
    # Points are not persisted yet, so the recomputation is stated, not hidden.
    assert BASELINE_RECOMPUTED_WARNING in response.warnings


def test_invalid_baseline_id_propagates_the_provider_error():
    from api.services.scenario_forecast_service import ScenarioComparisonErrorCode

    provider = FakeBaselineProvider(
        error=ScenarioComparisonError(
            ScenarioComparisonErrorCode.BASELINE_NOT_FOUND,
            "Baseline forecast 'bf_missing' was not found.",
            field_name="baseline_forecast_id",
        )
    )
    service = build_forecast_service(baseline_provider=provider)

    with pytest.raises(ScenarioComparisonError) as exc_info:
        service.create_comparison(build_request(baseline_forecast_id="bf_missing"))

    assert exc_info.value.error_code == "BASELINE_NOT_FOUND"


def test_prediction_failure_is_reported_as_a_comparison_error():
    predictor = FakeMatrixPredictor(error=RuntimeError("model exploded"))
    service = build_forecast_service(predictor=predictor)

    with pytest.raises(ScenarioComparisonError) as exc_info:
        service.create_comparison(build_request())

    error = exc_info.value
    assert error.error_code == "PREDICTION_FAILED"
    assert error.details["side"] == "baseline"
    assert "model exploded" in error.details["error"]


def test_comparison_response_round_trips_as_json():
    service = build_forecast_service(with_quantiles=True)

    payload = service.create_comparison(build_request()).model_dump(mode="json")

    assert payload["forecast_type"] == "predictive_what_if"
    assert payload["impact"]["total_difference"] == pytest.approx(150.0)
    assert len(payload["limitations"]) == 4
    restored = ScenarioComparisonResponse.model_validate(payload)
    assert restored.impact.horizon_days == HORIZON_DAYS


def test_baseline_and_scenario_cover_identical_dates():
    service = build_forecast_service()

    response = service.create_comparison(build_request())

    assert response.baseline.start_date == response.scenario.start_date
    assert response.baseline.end_date == response.scenario.end_date
    assert response.impact.dates[0] == FORECAST_START
    assert response.impact.dates[-1] == FORECAST_END
