"""Tests for the scenario-planning schemas and validation service."""

import os
import sys
from datetime import date, timedelta
from typing import List, Optional

import pytest
from pydantic import ValidationError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.schemas.scenario import (  # noqa: E402
    DEFAULT_CONFIDENCE_LEVELS,
    DEFAULT_MODEL_VERSION,
    MIN_HISTORY_ROWS,
    HistoryPoint,
    PromotionIntensity,
    ScenarioAssumptions,
    ScenarioCapabilitiesResponse,
    ScenarioComparisonResponse,
    ScenarioErrorResponse,
    ScenarioForecastPoint,
    ScenarioImpactSummary,
    ScenarioRequest,
    ScenarioType,
)
from api.services.scenario_config import (  # noqa: E402
    ENABLED_TYPES_ENV_VAR,
    get_capabilities_response,
    get_enabled_scenario_types,
    get_required_fields,
    is_scenario_supported,
)
from api.services.scenario_validation import (  # noqa: E402
    ScenarioErrorCode,
    ScenarioValidationError,
    ScenarioValidationService,
)

HISTORY_START = date(2026, 1, 1)


def build_history(
    rows: int = MIN_HISTORY_ROWS,
    start: date = HISTORY_START,
    sales: float = 420.0,
) -> List[HistoryPoint]:
    """Valid, chronologically sorted history with unique dates."""
    return [
        HistoryPoint(date=start + timedelta(days=offset), sales=sales + offset, store_id="store_001")
        for offset in range(rows)
    ]


def build_promotion_assumptions(
    start: date = date(2026, 1, 10),
    end: date = date(2026, 1, 14),
    **overrides,
) -> ScenarioAssumptions:
    """A valid promotion scenario."""
    payload = {
        "scenario_type": ScenarioType.PROMOTION,
        "start_date": start,
        "end_date": end,
        "promotion_intensity": PromotionIntensity.MEDIUM,
        "discount_depth": 0.25,
    }
    payload.update(overrides)
    return ScenarioAssumptions(**payload)


def build_request(
    history: Optional[List[HistoryPoint]] = None,
    scenario: Optional[ScenarioAssumptions] = None,
    **overrides,
) -> ScenarioRequest:
    """A valid scenario request, with targeted overrides for negative cases."""
    payload = {
        "store_id": "store_001",
        "horizon_days": 14,
        "history": history if history is not None else build_history(),
        "scenario": scenario if scenario is not None else build_promotion_assumptions(),
    }
    payload.update(overrides)
    return ScenarioRequest(**payload)


@pytest.fixture
def service() -> ScenarioValidationService:
    return ScenarioValidationService()


# --------------------------------------------------------------------------
# Valid promotion scenario
# --------------------------------------------------------------------------


def test_valid_promotion_scenario_parses_and_validates(service):
    request = build_request()

    assert request.store_id == "store_001"
    assert request.model_version == DEFAULT_MODEL_VERSION == "latest"
    assert request.confidence_levels == list(DEFAULT_CONFIDENCE_LEVELS)
    assert request.horizon_days == 14
    assert len(request.history) == MIN_HISTORY_ROWS
    assert request.scenario.scenario_type is ScenarioType.PROMOTION
    assert request.baseline_forecast_id is None
    assert request.last_history_date == HISTORY_START + timedelta(days=MIN_HISTORY_ROWS - 1)

    # Should not raise.
    service.validate_request(request)


def test_promotion_duration_is_derived_from_window():
    scenario = build_promotion_assumptions(start=date(2026, 1, 10), end=date(2026, 1, 14))
    assert scenario.duration_days == 5
    assert scenario.affects(date(2026, 1, 12))
    assert not scenario.affects(date(2026, 1, 20))
    assert len(scenario.window_dates) == 5


# --------------------------------------------------------------------------
# Invalid date range
# --------------------------------------------------------------------------


def test_invalid_date_range_rejected_by_schema():
    with pytest.raises(ValidationError) as exc_info:
        ScenarioAssumptions(
            scenario_type=ScenarioType.PROMOTION,
            start_date=date(2026, 1, 14),
            end_date=date(2026, 1, 10),
        )
    assert "end_date" in str(exc_info.value)


def test_invalid_date_range_rejected_by_service(service):
    scenario = ScenarioAssumptions.model_construct(
        scenario_type=ScenarioType.PROMOTION,
        start_date=date(2026, 1, 14),
        end_date=date(2026, 1, 10),
    )

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_scenario_dates(scenario)

    error = exc_info.value
    assert error.error_code == ScenarioErrorCode.INVALID_DATE_RANGE.value
    assert error.field == "scenario.end_date"
    assert error.details["start_date"] == "2026-01-14"
    assert error.details["end_date"] == "2026-01-10"


def test_duration_mismatch_rejected_by_service(service):
    scenario = ScenarioAssumptions.model_construct(
        scenario_type=ScenarioType.PROMOTION,
        start_date=date(2026, 1, 10),
        end_date=date(2026, 1, 14),
        duration_days=10,  # inside the horizon bound, but the window spans 5 days
    )

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_scenario_dates(scenario)

    error = exc_info.value
    assert error.error_code == ScenarioErrorCode.DURATION_MISMATCH.value
    assert error.details["duration_days"] == 10
    assert error.details["window_span_days"] == 5


def test_duration_exceeding_horizon_rejected_by_service(service):
    scenario = ScenarioAssumptions.model_construct(
        scenario_type=ScenarioType.PROMOTION, duration_days=120
    )

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_scenario_dates(scenario)
    assert exc_info.value.error_code == ScenarioErrorCode.INVALID_DURATION.value


# --------------------------------------------------------------------------
# Duplicate history dates
# --------------------------------------------------------------------------


def test_duplicate_history_dates_rejected_by_schema():
    history = build_history()
    history[3] = HistoryPoint(date=history[2].date, sales=500.0)

    with pytest.raises(ValidationError) as exc_info:
        build_request(history=history)
    assert "unique" in str(exc_info.value)


def test_duplicate_history_dates_rejected_by_service(service):
    history = build_history()
    history[3] = HistoryPoint(date=history[2].date, sales=500.0)

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_history(history)

    error = exc_info.value
    assert error.error_code == ScenarioErrorCode.HISTORY_DUPLICATE_DATES.value
    assert error.field == "history"
    assert error.details["duplicate_dates"] == [history[2].date.isoformat()]


def test_unsorted_history_rejected(service):
    history = build_history()
    history[2], history[5] = history[5], history[2]

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_history(history)
    assert exc_info.value.error_code == ScenarioErrorCode.HISTORY_NOT_SORTED.value

    with pytest.raises(ValidationError):
        build_request(history=history)


# --------------------------------------------------------------------------
# Negative sales
# --------------------------------------------------------------------------


def test_negative_sales_rejected_by_schema():
    with pytest.raises(ValidationError) as exc_info:
        HistoryPoint(date=HISTORY_START, sales=-1.0)
    assert "greater than or equal to 0" in str(exc_info.value)


def test_negative_sales_rejected_by_service(service):
    history = build_history()
    # model_construct bypasses field validation, proving the service is
    # defensively correct for objects it did not parse itself.
    history[4] = HistoryPoint.model_construct(date=history[4].date, sales=-25.0)

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_history(history)

    error = exc_info.value
    assert error.error_code == ScenarioErrorCode.HISTORY_NEGATIVE_SALES.value
    assert error.field == "history"
    assert error.details["violations"] == [
        {"index": 4, "date": history[4].date.isoformat(), "sales": -25.0}
    ]


# --------------------------------------------------------------------------
# Fewer than 7 history rows
# --------------------------------------------------------------------------


def test_short_history_rejected_by_schema():
    with pytest.raises(ValidationError) as exc_info:
        build_request(history=build_history(rows=6))
    assert "at least 7" in str(exc_info.value)


def test_short_history_rejected_by_service(service):
    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_history(build_history(rows=6))

    error = exc_info.value
    assert error.error_code == ScenarioErrorCode.HISTORY_TOO_SHORT.value
    assert error.details == {"min_history_rows": 7, "received": 6}


def test_empty_history_rejected_by_service(service):
    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_history([])
    assert exc_info.value.error_code == ScenarioErrorCode.HISTORY_EMPTY.value


# --------------------------------------------------------------------------
# Invalid confidence levels
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "levels,expected_code",
    [
        ([0.1, 1.5, 0.9], ScenarioErrorCode.CONFIDENCE_LEVEL_OUT_OF_RANGE),
        ([0.0, 0.5, 0.9], ScenarioErrorCode.CONFIDENCE_LEVEL_OUT_OF_RANGE),
        ([0.1, 0.5, 1.0], ScenarioErrorCode.CONFIDENCE_LEVEL_OUT_OF_RANGE),
        ([-0.2, 0.5], ScenarioErrorCode.CONFIDENCE_LEVEL_OUT_OF_RANGE),
        ([0.5, 0.5, 0.9], ScenarioErrorCode.CONFIDENCE_LEVELS_NOT_UNIQUE),
        ([0.9, 0.5, 0.1], ScenarioErrorCode.CONFIDENCE_LEVELS_NOT_SORTED),
        ([], ScenarioErrorCode.CONFIDENCE_LEVELS_EMPTY),
    ],
)
def test_invalid_confidence_levels_rejected_by_service(service, levels, expected_code):
    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_confidence_levels(levels)

    error = exc_info.value
    assert error.error_code == expected_code.value
    assert error.field == "confidence_levels"
    assert error.details


@pytest.mark.parametrize(
    "levels",
    [[0.1, 1.5, 0.9], [0.5, 0.5, 0.9], [0.9, 0.5, 0.1], []],
)
def test_invalid_confidence_levels_rejected_by_schema(levels):
    with pytest.raises(ValidationError):
        build_request(confidence_levels=levels)


def test_valid_confidence_levels_accepted(service):
    service.validate_confidence_levels([0.05, 0.5, 0.95])


# --------------------------------------------------------------------------
# Unsupported scenario type
# --------------------------------------------------------------------------


def test_unknown_scenario_type_rejected_by_schema():
    with pytest.raises(ValidationError) as exc_info:
        ScenarioAssumptions(scenario_type="weather")
    assert "scenario_type" in str(exc_info.value)


def test_disabled_scenario_type_rejected_by_service():
    service = ScenarioValidationService(
        enabled_scenario_types=[ScenarioType.PROMOTION, ScenarioType.TRAFFIC]
    )

    service.validate_scenario_support(ScenarioType.PROMOTION)

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_scenario_support(ScenarioType.CUSTOM)

    error = exc_info.value
    assert error.error_code == ScenarioErrorCode.UNSUPPORTED_SCENARIO_TYPE.value
    assert error.field == "scenario.scenario_type"
    assert error.details["scenario_type"] == "custom"
    assert error.details["supported_scenario_types"] == ["promotion", "traffic"]


def test_validate_request_surfaces_unsupported_type_first():
    service = ScenarioValidationService(enabled_scenario_types=[ScenarioType.TRAFFIC])

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_request(build_request())
    assert exc_info.value.error_code == ScenarioErrorCode.UNSUPPORTED_SCENARIO_TYPE.value


# --------------------------------------------------------------------------
# Closure scenarios
# --------------------------------------------------------------------------


def test_closure_date_in_past_rejected_by_schema():
    history = build_history()
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CLOSURE,
        closure_dates=[HISTORY_START],  # before the latest history date
    )

    with pytest.raises(ValidationError) as exc_info:
        build_request(history=history, scenario=scenario)
    assert "past" in str(exc_info.value)


def test_closure_date_in_past_rejected_by_service(service):
    history = build_history()
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CLOSURE, closure_dates=[HISTORY_START]
    )

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_scenario_dates(scenario, last_history_date=history[-1].date)

    error = exc_info.value
    assert error.error_code == ScenarioErrorCode.CLOSURE_DATE_IN_PAST.value
    assert error.details["past_dates"] == [HISTORY_START.isoformat()]


def test_closure_scenario_requires_closure_dates(service):
    scenario = ScenarioAssumptions(scenario_type=ScenarioType.CLOSURE)

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_scenario_dates(scenario)

    error = exc_info.value
    assert error.error_code == ScenarioErrorCode.MISSING_SCENARIO_FIELD.value
    assert error.details["missing_fields"] == ["closure_dates"]


def test_duplicate_closure_dates_rejected_by_schema():
    with pytest.raises(ValidationError) as exc_info:
        ScenarioAssumptions(
            scenario_type=ScenarioType.CLOSURE,
            closure_dates=[date(2026, 2, 1), date(2026, 2, 1)],
        )
    assert "duplicates" in str(exc_info.value)


def test_valid_closure_scenario_validates(service):
    history = build_history()
    scenario = ScenarioAssumptions(
        scenario_type=ScenarioType.CLOSURE,
        closure_dates=[date(2026, 1, 20), date(2026, 1, 18)],
    )
    # Closure dates are normalised to ascending order.
    assert scenario.closure_dates == [date(2026, 1, 18), date(2026, 1, 20)]

    service.validate_scenario_dates(scenario, last_history_date=history[-1].date)


# --------------------------------------------------------------------------
# Horizon and bounds
# --------------------------------------------------------------------------


@pytest.mark.parametrize("horizon_days", [0, -5, 91, 1000])
def test_out_of_range_horizon_rejected(service, horizon_days):
    with pytest.raises(ValidationError):
        build_request(horizon_days=horizon_days)

    with pytest.raises(ScenarioValidationError) as exc_info:
        service.validate_horizon(horizon_days)
    assert exc_info.value.error_code == ScenarioErrorCode.INVALID_HORIZON.value


@pytest.mark.parametrize("multiplier", [0.4, 2.1])
def test_traffic_multiplier_bounds_enforced(multiplier):
    with pytest.raises(ValidationError):
        ScenarioAssumptions(
            scenario_type=ScenarioType.TRAFFIC,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 1, 14),
            traffic_multiplier=multiplier,
        )


@pytest.mark.parametrize("discount_depth", [-0.1, 1.1])
def test_discount_depth_bounds_enforced(discount_depth):
    with pytest.raises(ValidationError):
        build_promotion_assumptions(discount_depth=discount_depth)


def test_unknown_request_field_is_rejected():
    with pytest.raises(ValidationError):
        build_request(unexpected_field="nope")


# --------------------------------------------------------------------------
# Response models
# --------------------------------------------------------------------------


def test_forecast_point_build_derives_delta():
    point = ScenarioForecastPoint.build(
        day=date(2026, 1, 10),
        baseline_sales=400.0,
        scenario_sales=500.0,
        quantiles={"0.1": 450.0, "0.9": 550.0},
        in_scenario_window=True,
    )

    assert point.delta == 100.0
    assert point.delta_pct == pytest.approx(25.0)
    assert point.in_scenario_window is True
    assert point.quantiles["0.9"] == 550.0


def test_forecast_point_build_handles_zero_baseline():
    point = ScenarioForecastPoint.build(
        day=date(2026, 1, 10), baseline_sales=0.0, scenario_sales=120.0
    )
    assert point.delta == 120.0
    assert point.delta_pct is None


def test_impact_summary_from_points():
    points = [
        ScenarioForecastPoint.build(
            day=date(2026, 1, 10) + timedelta(days=offset),
            baseline_sales=100.0,
            scenario_sales=100.0 + offset * 10.0,
            in_scenario_window=offset < 2,
        )
        for offset in range(3)
    ]

    summary = ScenarioImpactSummary.from_points(points)

    assert summary.horizon_days == 3
    assert summary.baseline_total == pytest.approx(300.0)
    assert summary.scenario_total == pytest.approx(330.0)
    assert summary.absolute_delta == pytest.approx(30.0)
    assert summary.percent_delta == pytest.approx(10.0)
    assert summary.average_daily_delta == pytest.approx(10.0)
    assert summary.peak_delta == pytest.approx(20.0)
    assert summary.peak_delta_date == date(2026, 1, 12)
    assert summary.days_in_scenario_window == 2


def test_impact_summary_handles_empty_points():
    summary = ScenarioImpactSummary.from_points([])
    assert summary.horizon_days == 0
    assert summary.absolute_delta == 0.0
    assert summary.percent_delta is None
    assert summary.peak_delta_date is None


def test_comparison_response_round_trips():
    points = [
        ScenarioForecastPoint.build(
            day=date(2026, 1, 10), baseline_sales=400.0, scenario_sales=460.0
        )
    ]
    response = ScenarioComparisonResponse(
        scenario_id="scn_123",
        store_id="store_001",
        model_version="latest",
        scenario_type=ScenarioType.PROMOTION,
        horizon_days=1,
        confidence_levels=list(DEFAULT_CONFIDENCE_LEVELS),
        assumptions=build_promotion_assumptions(),
        points=points,
        summary=ScenarioImpactSummary.from_points(points),
        warnings=["Promotion uplift uses planning defaults, not fitted effects."],
    )

    payload = response.model_dump(mode="json")
    assert payload["scenario_type"] == "promotion"
    assert payload["schema_version"] == "v1"
    assert payload["generated_at"]
    assert ScenarioComparisonResponse.model_validate(payload).scenario_id == "scn_123"


def test_error_response_from_validation_error():
    error = ScenarioValidationError(
        ScenarioErrorCode.HISTORY_TOO_SHORT,
        "history must contain at least 7 rows, received 3",
        field="history",
        details={"min_history_rows": 7, "received": 3},
    )

    response = error.to_response()
    assert isinstance(response, ScenarioErrorResponse)
    assert response.error_code == "history_too_short"
    assert response.field == "history"
    assert response.details["received"] == 3
    assert error.to_dict() == response.model_dump()


# --------------------------------------------------------------------------
# Capability configuration
# --------------------------------------------------------------------------


def test_capabilities_response_defaults():
    response = get_capabilities_response()

    assert isinstance(response, ScenarioCapabilitiesResponse)
    assert response.min_history_rows == MIN_HISTORY_ROWS
    assert response.max_horizon_days == 90
    assert response.default_model_version == "latest"
    assert response.default_confidence_levels == list(DEFAULT_CONFIDENCE_LEVELS)
    assert len(response.capabilities) == len(ScenarioType)
    assert set(response.supported_scenario_types) == set(ScenarioType)


def test_required_fields_per_scenario_type():
    assert get_required_fields(ScenarioType.CLOSURE) == ["closure_dates"]
    assert "traffic_multiplier" in get_required_fields(ScenarioType.TRAFFIC)
    assert get_required_fields(ScenarioType.CUSTOM) == ["custom_overrides"]


def test_enabled_scenario_types_can_be_restricted_by_env(monkeypatch):
    monkeypatch.setenv(ENABLED_TYPES_ENV_VAR, "promotion, closure")

    assert get_enabled_scenario_types() == (ScenarioType.PROMOTION, ScenarioType.CLOSURE)
    assert is_scenario_supported(ScenarioType.PROMOTION)
    assert not is_scenario_supported(ScenarioType.TRAFFIC)

    response = get_capabilities_response()
    assert set(response.supported_scenario_types) == {
        ScenarioType.PROMOTION,
        ScenarioType.CLOSURE,
    }


def test_unknown_env_scenario_types_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv(ENABLED_TYPES_ENV_VAR, "weather, tariffs")
    assert get_enabled_scenario_types() == tuple(ScenarioType)

    monkeypatch.setenv(ENABLED_TYPES_ENV_VAR, "  ")
    assert get_enabled_scenario_types() == tuple(ScenarioType)
