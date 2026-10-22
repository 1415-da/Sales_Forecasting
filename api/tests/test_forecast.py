"""
Forecast generation: request contract, recursion, uncertainty, and failures.

Covers both layers deliberately. The service tests pin the numerical behaviour
-- that recursion really feeds predictions forward, that the band widens, that
reconstructed inputs are declared. The route tests pin the HTTP contract -- the
documented response shape and the status for each failure.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.core.errors import (
    FeatureMismatchError,
    InsufficientHistoryError,
    PredictionFailedError,
    ScenarioValidationError,
)
from api.deps import get_forecast_service
from api.main import create_app
from api.schemas.forecast import ForecastRequest, HistoryObservation
from api.services.feature_service import FeatureBuilder, is_us_holiday
from api.services.forecast_service import (
    HISTORY_VOLATILITY_WARNING,
    MIN_RESIDUAL_FRACTION,
    UNCERTAINTY_METHOD_HISTORY,
    UNCERTAINTY_METHOD_RESIDUAL,
)

from .conftest import (
    TRAINED_FEATURE_COLS,
    ConstantEstimator,
    EchoLagEstimator,
    ExplodingEstimator,
    make_history,
    make_request,
)

# --------------------------------------------------------------------------
# Request contract
# --------------------------------------------------------------------------


def test_history_must_be_ordered_oldest_first():
    """Out-of-order history is rejected: lag features are positional."""
    history = make_history(days=35)
    history[10], history[11] = history[11], history[10]
    with pytest.raises(ValidationError, match="oldest-first"):
        make_request(history=history)


def test_duplicate_history_dates_are_rejected():
    """A repeated day would silently shift every rolling window."""
    history = make_history(days=35)
    history[5] = HistoryObservation(date=history[4].date, sales=1.0)
    with pytest.raises(ValidationError, match="duplicate history date"):
        make_request(history=history)


@pytest.mark.parametrize("horizon", [0, 91, 365])
def test_horizon_outside_the_supported_range_is_rejected(horizon):
    """Only 1..90 days are offered."""
    with pytest.raises(ValidationError):
        make_request(horizon_days=horizon)


@pytest.mark.parametrize("level", [0.5, 0.9, 0.99])
def test_unsupported_confidence_levels_are_rejected(level):
    """The band is only calibrated for the two levels on offer."""
    with pytest.raises(ValidationError, match="confidence_level"):
        make_request(confidence_level=level)


def test_negative_sales_are_rejected():
    """Sales cannot be negative, so the schema refuses it."""
    with pytest.raises(ValidationError):
        ForecastRequest(
            store_id="CA_1",
            history=[HistoryObservation(date=date(2026, 1, 1), sales=-1.0)],
        )


# --------------------------------------------------------------------------
# Feature contract
# --------------------------------------------------------------------------


def test_builder_covers_every_trained_feature(settings, bundle_factory):
    """
    Every one of the 51 trained features must be derivable.

    If training adds a feature family this builder cannot produce, this is
    where it should be noticed.
    """
    builder = FeatureBuilder(bundle_factory(), settings)
    assert builder.unknown_features == []
    builder.assert_supported()

    series = builder.read_history(make_history(days=60))
    policy = builder.build_policy(series)
    row = builder.build_row(date(2026, 10, 21), series.sales, 0.0, policy)
    assert sorted(row) == sorted(TRAINED_FEATURE_COLS)


def test_unknown_feature_family_is_refused(settings, bundle_factory):
    """A feature the builder cannot derive is an error, never a zero."""
    bundle = bundle_factory(
        feature_cols=TRAINED_FEATURE_COLS + ["competitor_price_index"]
    )
    builder = FeatureBuilder(bundle, settings)
    assert builder.unknown_features == ["competitor_price_index"]
    with pytest.raises(FeatureMismatchError) as caught:
        builder.assert_supported()
    assert "competitor_price_index" in caught.value.details["unsupported_features"]


def test_required_history_days_follows_the_deepest_window(settings, bundle_factory):
    """The contract's depth is the widest lag or rolling window: 30 days."""
    builder = FeatureBuilder(bundle_factory(), settings)
    assert builder.required_history_days == 30


def test_same_day_features_are_rebuilt_from_the_previous_level(settings, bundle_factory):
    """
    ``quantity_sold`` and ``profit`` track the last known sales value.

    They are algebraically tied to the target in the training frame, so this
    relationship -- not a constant -- is what keeps the model in distribution.
    """
    builder = FeatureBuilder(bundle_factory(), settings)
    series = builder.read_history(make_history(days=60, level=3000.0))
    policy = builder.build_policy(series)
    last = series.sales[-1]

    row = builder.build_row(date(2026, 10, 21), series.sales, 0.0, policy)
    assert row["quantity_sold"] == pytest.approx(last)
    assert row["profit"] == pytest.approx(0.3 * last)


def test_supplied_exogenous_columns_override_the_reconstruction(settings, bundle_factory):
    """A caller who knows the real ratio gets it used instead of the default."""
    builder = FeatureBuilder(bundle_factory(), settings)
    history = make_history(days=60, level=1000.0, profit=250.0, customer_traffic=400.0)
    series = builder.read_history(history)
    policy = builder.build_policy(series)

    assert "profit" not in policy.imputed
    assert "customer_traffic" not in policy.imputed
    # The ratio is pairwise over the whole window (total profit / total sales),
    # not an average of per-day ratios, so a varying sales series does not bias
    # it towards the low-sales days.
    expected = (250.0 * len(history)) / sum(item.sales for item in history)
    assert policy.profit_to_sales_ratio == pytest.approx(expected, rel=1e-9)
    assert policy.profit_to_sales_ratio != pytest.approx(0.3)
    assert policy.carried_values["customer_traffic"] == pytest.approx(400.0)


def test_features_constant_in_training_stay_at_that_constant(settings, bundle_factory):
    """
    An unsupplied ``customer_traffic`` is zero, as it was during training.

    The previous stack sent 500, which the model never saw.
    """
    builder = FeatureBuilder(bundle_factory(), settings)
    series = builder.read_history(make_history(days=60))
    policy = builder.build_policy(series)
    assert policy.carried_values["customer_traffic"] == 0.0
    assert policy.carried_values["has_promotion"] == 0.0
    assert set(policy.imputed) == {
        "quantity_sold",
        "profit",
        "customer_traffic",
        "has_promotion",
    }


def test_is_holiday_comes_from_the_public_holiday_calendar(settings, bundle_factory):
    """
    ``is_holiday`` is reproduced from the calendar training used.

    The training pipeline overwrites the merged column with ``holidays.US()``,
    so a forecast for 4 July must carry the flag.
    """
    builder = FeatureBuilder(bundle_factory(), settings)
    series = builder.read_history(make_history(days=60))
    policy = builder.build_policy(series)

    independence_day = date(2027, 7, 4)
    ordinary_day = date(2027, 7, 7)
    assert is_us_holiday(independence_day) is True

    holiday_row = builder.build_row(independence_day, series.sales, 0.0, policy)
    ordinary_row = builder.build_row(ordinary_day, series.sales, 0.0, policy)
    assert holiday_row["is_holiday"] == 1.0
    assert ordinary_row["is_holiday"] == 0.0


def test_rolling_std_matches_the_training_convention(settings, bundle_factory):
    """
    Rolling std is the sample (ddof=1) statistic pandas produced in training.

    A population std would be subtly wrong for every rolling column.
    """
    builder = FeatureBuilder(bundle_factory(), settings)
    sales = [10.0, 20.0, 30.0]
    features = builder.rolling_features(sales)
    # Sample std of [10, 20, 30] is 10.0; the population std is ~8.165.
    assert features["sales_rolling_3_std"] == pytest.approx(10.0)


def test_unknown_store_is_refused(settings, bundle_factory):
    """
    A store the encoder does not know cannot be silently substituted.

    Mapping it to an arbitrary class would return a confident forecast for a
    different store.
    """
    builder = FeatureBuilder(bundle_factory(), settings)
    with pytest.raises(ScenarioValidationError) as caught:
        builder.encode_store("ZZ_9")
    assert "CA_1" in caught.value.details["supported_stores"]


# --------------------------------------------------------------------------
# History validation
# --------------------------------------------------------------------------


def test_too_little_history_is_a_typed_error(settings, bundle_factory):
    """Below the configured minimum, the request cannot be served."""
    builder = FeatureBuilder(bundle_factory(), settings)
    series = builder.read_history(make_history(days=10))
    with pytest.raises(InsufficientHistoryError) as caught:
        builder.validate_history(series)
    assert caught.value.status_code == 422
    assert caught.value.details["required_days"] == 30
    assert caught.value.details["supplied_days"] == 10


def test_history_shorter_than_recommended_warns_but_proceeds(settings, bundle_factory):
    """30-59 days is usable; the thinner rolling window is disclosed."""
    builder = FeatureBuilder(bundle_factory(), settings)
    warnings = builder.validate_history(builder.read_history(make_history(days=35)))
    assert any("fuller window" in warning for warning in warnings)


def test_history_gaps_are_reported(settings, bundle_factory):
    """Missing calendar days shift positional windows, so they are flagged."""
    builder = FeatureBuilder(bundle_factory(), settings)
    history = make_history(days=60)
    del history[20:23]
    warnings = builder.validate_history(builder.read_history(history))
    assert any("missing calendar day" in warning for warning in warnings)


def test_all_zero_history_is_refused(settings, bundle_factory):
    """With no positive sales there is no level to project."""
    builder = FeatureBuilder(bundle_factory(), settings)
    history = [
        HistoryObservation(date=date(2026, 8, 1) + timedelta(days=offset), sales=0.0)
        for offset in range(40)
    ]
    with pytest.raises(ScenarioValidationError, match="no positive sales"):
        builder.validate_history(builder.read_history(history))


# --------------------------------------------------------------------------
# Forecast generation
# --------------------------------------------------------------------------


def test_seven_day_forecast_has_the_documented_shape(forecast_service):
    """The happy path: contiguous dates from the day after history ends."""
    service = forecast_service()
    request = make_request(horizon_days=7)
    response = service.generate(request)

    assert len(response.forecasts) == 7
    expected_first = request.history_end_date + timedelta(days=1)
    assert response.forecasts[0].date == expected_first
    assert response.forecasts[-1].date == expected_first + timedelta(days=6)
    assert [item.date for item in response.forecasts] == [
        expected_first + timedelta(days=offset) for offset in range(7)
    ]
    assert response.model_stage == "Production"
    assert response.model_version == "1"
    assert response.forecast_id.startswith("fc_")
    assert response.metadata.history_days == 60
    assert response.metadata.feature_count == len(TRAINED_FEATURE_COLS)
    assert response.metadata.run_id == "run-1"
    assert response.metadata.data_source == "kaggle_m5"


def test_thirty_day_forecast_summary_is_consistent(forecast_service):
    """Totals must equal the sum of the daily series they summarise."""
    service = forecast_service()
    response = service.generate(make_request(horizon_days=30))

    assert len(response.forecasts) == 30
    assert response.summary.total_forecast_sales == pytest.approx(
        sum(item.predicted_sales for item in response.forecasts), rel=1e-6
    )
    assert response.summary.average_daily_sales == pytest.approx(
        response.summary.total_forecast_sales / 30, rel=1e-3
    )
    assert response.summary.lower_bound_total <= response.summary.total_forecast_sales
    assert response.summary.upper_bound_total >= response.summary.total_forecast_sales


def test_predictions_feed_forward(forecast_service, bundle_factory):
    """
    Recursion is real, not simulated.

    The estimator returns ``sales_lag_1``, so with a persistence model every
    forecast day must equal the last observed value -- which is only true if
    each prediction is appended to the series the next row is built from.
    """
    estimator = EchoLagEstimator(TRAINED_FEATURE_COLS)
    service = forecast_service(bundle_factory(model=estimator))
    request = make_request(horizon_days=10)
    response = service.generate(request)

    last_observed = request.history[-1].sales
    for item in response.forecasts:
        assert item.predicted_sales == pytest.approx(last_observed, rel=1e-6)
    assert len(estimator.seen_frames) == 10


def test_recursion_compounds_a_growing_estimator(forecast_service, bundle_factory):
    """A 1%-per-step estimator must compound, confirming the feedback loop."""
    estimator = EchoLagEstimator(TRAINED_FEATURE_COLS, multiplier=1.01)
    service = forecast_service(bundle_factory(model=estimator))
    request = make_request(horizon_days=5)
    response = service.generate(request)

    base = request.history[-1].sales
    for step, item in enumerate(response.forecasts, start=1):
        assert item.predicted_sales == pytest.approx(base * (1.01**step), rel=1e-6)


def test_interval_widens_with_horizon(forecast_service, bundle_factory):
    """
    A recursive forecast inherits its own error, so the band must widen.

    With a constant point forecast the widening is attributable to the horizon
    scaling alone.
    """
    service = forecast_service(bundle_factory(model=ConstantEstimator(2000.0)))
    response = service.generate(make_request(horizon_days=30))

    widths = [item.upper_bound - item.lower_bound for item in response.forecasts]
    assert widths == sorted(widths)
    assert widths[-1] > widths[0]
    for item in response.forecasts:
        assert item.lower_bound <= item.predicted_sales <= item.upper_bound


def test_lower_bound_never_goes_negative(forecast_service, bundle_factory):
    """Sales cannot be negative, so the band is clamped at zero."""
    service = forecast_service(bundle_factory(model=ConstantEstimator(5.0)))
    response = service.generate(make_request(horizon_days=30))
    assert all(item.lower_bound >= 0.0 for item in response.forecasts)


def test_eighty_percent_band_is_narrower_than_ninety_five(forecast_service, bundle_factory):
    """The confidence level has to actually change the interval."""
    narrow = forecast_service(bundle_factory(model=ConstantEstimator(2000.0))).generate(
        make_request(horizon_days=14, confidence_level=0.8)
    )
    wide = forecast_service(bundle_factory(model=ConstantEstimator(2000.0))).generate(
        make_request(horizon_days=14, confidence_level=0.95)
    )
    narrow_width = narrow.forecasts[0].upper_bound - narrow.forecasts[0].lower_bound
    wide_width = wide.forecasts[0].upper_bound - wide.forecasts[0].lower_bound
    assert narrow_width < wide_width


def test_uncertainty_uses_the_models_recorded_error(forecast_service):
    """
    With an RMSE on the run, the band is a residual interval.

    The ensemble's recorded RMSE is 98.08 and the forecast level is ~3000, so
    the value is inside the plausible band and used as recorded.
    """
    service = forecast_service()
    response = service.generate(make_request(horizon_days=7))
    assert response.metadata.uncertainty_method == UNCERTAINTY_METHOD_RESIDUAL
    assert response.metadata.residual_std == pytest.approx(98.08, rel=1e-3)
    assert not any(
        HISTORY_VOLATILITY_WARNING == warning for warning in response.warnings
    )


def test_uncertainty_falls_back_to_history_volatility(
    settings, fake_client, bundle_factory, run_metrics
):
    """
    Without a recorded error, the fallback is used *and* disclosed.

    A band derived from history volatility describes the data, not the model,
    and the response has to say so.
    """
    from api.services.forecast_service import ForecastService
    from api.services.model_registry_service import ModelRegistryService

    from .conftest import FakeArtifactService

    fake_client.runs["run-1"].data.metrics = {}
    registry = ModelRegistryService(
        settings=settings, client_factory=lambda: fake_client
    )
    service = ForecastService(
        settings=settings,
        registry=registry,
        artifacts=FakeArtifactService(bundle_factory(model=ConstantEstimator(2000.0))),
    )
    response = service.generate(make_request(horizon_days=7))

    assert response.metadata.uncertainty_method == UNCERTAINTY_METHOD_HISTORY
    assert HISTORY_VOLATILITY_WARNING in response.warnings


def test_a_tiny_recorded_error_is_floored(forecast_service, bundle_factory, fake_client):
    """
    A near-zero RMSE must not produce a zero-width band.

    A recursive forecast is never that precise, and a band of zero width would
    misrepresent it as certain.
    """
    fake_client.runs["run-1"].data.metrics["ensemble_rmse"] = 0.0001
    service = forecast_service(bundle_factory(model=ConstantEstimator(2000.0)))
    response = service.generate(make_request(horizon_days=7))
    assert response.metadata.residual_std >= MIN_RESIDUAL_FRACTION * 2000.0 * 0.99


def test_reconstructed_inputs_are_declared(forecast_service):
    """The response states which inputs it had to rebuild, and why it matters."""
    service = forecast_service()
    response = service.generate(make_request(horizon_days=7))
    assert set(response.metadata.imputed_features) == {
        "quantity_sold",
        "profit",
        "customer_traffic",
        "has_promotion",
    }
    assert any("quantity_sold equals sales" in warning for warning in response.warnings)
    assert any("grows across the horizon" in warning for warning in response.warnings)


# --------------------------------------------------------------------------
# Leak-free feature contract (is_closed, snap_day, no same-day columns)
# --------------------------------------------------------------------------

#: The contract a model trained without same-day target columns carries.
LEAK_FREE_FEATURE_COLS = [
    column
    for column in TRAINED_FEATURE_COLS
    if column not in ("quantity_sold", "profit", "has_promotion", "customer_traffic")
] + ["is_closed", "snap_day"]


def test_leak_free_contract_is_fully_buildable(settings, bundle_factory):
    """Every column of the new contract is derivable, and nothing else is added."""
    builder = FeatureBuilder(bundle_factory(feature_cols=LEAK_FREE_FEATURE_COLS), settings)
    assert builder.unknown_features == []
    assert builder.uses_same_day_columns is False

    series = builder.read_history(make_history(days=60))
    policy = builder.build_policy(series, store_id="CA_1")
    row = builder.build_row(date(2026, 10, 21), series.sales, 0.0, policy)
    assert sorted(row) == sorted(LEAK_FREE_FEATURE_COLS)


def test_christmas_is_a_closure_not_a_holiday(settings, bundle_factory):
    """
    Training zeroes ``is_holiday`` on a closed day, so serving must as well.

    Otherwise the model would see a combination it never trained on.
    """
    builder = FeatureBuilder(bundle_factory(feature_cols=LEAK_FREE_FEATURE_COLS), settings)
    series = builder.read_history(make_history(days=60))
    policy = builder.build_policy(series, store_id="CA_1")

    christmas = builder.build_row(date(2026, 12, 25), series.sales, 0.0, policy)
    thanksgiving = builder.build_row(date(2026, 11, 26), series.sales, 0.0, policy)
    assert (christmas["is_closed"], christmas["is_holiday"]) == (1.0, 0.0)
    assert (thanksgiving["is_closed"], thanksgiving["is_holiday"]) == (0.0, 1.0)


def test_christmas_stays_a_holiday_for_a_model_without_is_closed(settings, bundle_factory):
    """A model trained before ``is_closed`` existed keeps its own inputs exactly."""
    builder = FeatureBuilder(bundle_factory(), settings)
    series = builder.read_history(make_history(days=60))
    policy = builder.build_policy(series)
    row = builder.build_row(date(2026, 12, 25), series.sales, 0.0, policy)
    assert row["is_holiday"] == 1.0
    assert "is_closed" not in row


@pytest.mark.parametrize(
    ("store_id", "snap_dates", "other_dates"),
    [
        ("CA_2", [date(2026, 11, 1), date(2026, 11, 10)], [date(2026, 11, 11)]),
        ("TX_1", [date(2026, 11, 3), date(2026, 11, 15)], [date(2026, 11, 2)]),
        ("WI_3", [date(2026, 11, 2), date(2026, 11, 14)], [date(2026, 11, 1)]),
    ],
)
def test_snap_day_follows_the_store_state_schedule(
    settings, bundle_factory, store_id, snap_dates, other_dates
):
    """SNAP days depend on the store's state, not just the date."""
    builder = FeatureBuilder(bundle_factory(feature_cols=LEAK_FREE_FEATURE_COLS), settings)
    series = builder.read_history(make_history(days=60))
    policy = builder.build_policy(series, store_id=store_id)
    for day in snap_dates:
        assert builder.build_row(day, series.sales, 0.0, policy)["snap_day"] == 1.0
    for day in other_dates:
        assert builder.build_row(day, series.sales, 0.0, policy)["snap_day"] == 0.0


def test_snap_schedule_matches_the_m5_calendar():
    """
    The hard-coded schedule is the one training learned from.

    Training reads SNAP from M5's ``calendar.csv``; serving cannot, so it uses
    ``SNAP_DAYS_BY_STATE``. This pins the two together wherever the dataset is
    present.
    """
    import csv
    from pathlib import Path

    from api.services.feature_service import SNAP_DAYS_BY_STATE

    calendar = (
        Path(__file__).resolve().parents[2]
        / "data" / "raw" / "kaggle_m5" / "v1.0.0" / "calendar.csv"
    )
    if not calendar.exists():
        pytest.skip("M5 calendar.csv is not available in this checkout")

    observed = {state: set() for state in SNAP_DAYS_BY_STATE}
    with calendar.open() as handle:
        for record in csv.DictReader(handle):
            day = int(record["date"][-2:])
            for state in observed:
                if record[f"snap_{state}"] == "1":
                    observed[state].add(day)
    assert observed == {state: set(days) for state, days in SNAP_DAYS_BY_STATE.items()}


class _ClosureAwareEstimator:
    """Sells nothing on a closed day, otherwise repeats yesterday (persistence)."""

    def predict(self, frame):
        return [
            0.0 if closed >= 0.5 else float(lag)
            for closed, lag in zip(frame["is_closed"], frame["sales_lag_1"])
        ]


def test_a_closed_day_does_not_drag_down_the_days_after_it(forecast_service, bundle_factory):
    """
    Christmas forecasts as ~0, but the day after starts from the open level.

    Feeding the closed day's 0 into lag-1 would make a persistence model forecast
    0 for every day after Christmas -- the artefact this rule exists to stop.
    """
    service = forecast_service(
        bundle_factory(model=_ClosureAwareEstimator(), feature_cols=LEAK_FREE_FEATURE_COLS)
    )
    history = make_history(days=60, end=date(2026, 12, 22), level=3000.0)
    response = service.generate(make_request(history=history, horizon_days=6))

    by_date = {point.date: point.predicted_sales for point in response.forecasts}
    level = history[-1].sales
    assert by_date[date(2026, 12, 25)] == 0.0
    assert by_date[date(2026, 12, 26)] == pytest.approx(level)
    assert by_date[date(2026, 12, 27)] == pytest.approx(level)


def test_leakage_warning_is_only_given_for_a_leaky_model(forecast_service, bundle_factory):
    """Telling a user a model reads the answer back when it does not is wrong too."""
    leak_free = forecast_service(bundle_factory(feature_cols=LEAK_FREE_FEATURE_COLS))
    response = leak_free.generate(make_request(horizon_days=7))
    assert not any("quantity_sold equals sales" in warning for warning in response.warnings)
    assert response.metadata.imputed_features == []


def test_the_bundle_is_loaded_for_the_resolved_run(forecast_service):
    """Artifacts must come from the version the registry resolved."""
    service = forecast_service()
    service.generate(make_request())
    assert service.test_artifacts.calls == [("run-1", "ensemble")]


def test_a_failing_estimator_becomes_a_typed_error(forecast_service, bundle_factory):
    """
    An estimator blowing up is a 500 with no internals leaked.

    The underlying message is kept for the log, not the envelope.
    """
    service = forecast_service(bundle_factory(model=ExplodingEstimator()))
    with pytest.raises(PredictionFailedError) as caught:
        service.generate(make_request(horizon_days=3))
    assert caught.value.status_code == 500
    assert caught.value.details["model"] == "ensemble"


# --------------------------------------------------------------------------
# HTTP contract
# --------------------------------------------------------------------------


@pytest.fixture
def client(forecast_service):
    """An app whose forecast service is the fake one."""
    service = forecast_service()
    app = create_app()
    app.dependency_overrides[get_forecast_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client


def _payload(**overrides):
    """A JSON body for ``POST /v1/forecast``."""
    body = {
        "store_id": "CA_1",
        "model": "ensemble",
        "model_version": "latest",
        "horizon_days": 7,
        "confidence_level": 0.95,
        "history": [
            {"date": item.date.isoformat(), "sales": item.sales}
            for item in make_history()
        ],
    }
    body.update(overrides)
    return body


def test_post_forecast_returns_the_documented_body(client):
    """Every documented key is present, with dates as ISO strings."""
    response = client.post("/v1/forecast", json=_payload())
    assert response.status_code == 200
    body = response.json()

    assert set(body) >= {
        "forecast_id",
        "store_id",
        "model",
        "model_version",
        "model_stage",
        "generated_at",
        "horizon_days",
        "confidence_level",
        "summary",
        "forecasts",
        "metadata",
    }
    assert set(body["summary"]) == {
        "total_forecast_sales",
        "average_daily_sales",
        "lower_bound_total",
        "upper_bound_total",
    }
    assert set(body["forecasts"][0]) == {
        "date",
        "predicted_sales",
        "lower_bound",
        "upper_bound",
    }
    assert set(body["metadata"]) >= {
        "history_start_date",
        "history_end_date",
        "history_days",
        "feature_schema_version",
    }
    assert body["metadata"]["history_days"] == 60
    assert body["warnings"]


def test_post_forecast_echoes_a_request_id(client):
    """The request id is echoed so a user can quote it in a bug report."""
    response = client.post(
        "/v1/forecast", json=_payload(), headers={"X-Request-ID": "req_abc123"}
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_abc123"


def test_insufficient_history_is_422_with_the_specific_code(client):
    """The UI needs to distinguish "add more history" from any other 422."""
    short = [
        {"date": item.date.isoformat(), "sales": item.sales}
        for item in make_history(days=5)
    ]
    response = client.post("/v1/forecast", json=_payload(history=short))
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INSUFFICIENT_HISTORY"
    assert body["error"]["details"]["required_days"] == 30


def test_unknown_store_is_422_and_lists_the_supported_stores(client):
    """An unusable store comes back with the options, so the UI can recover."""
    response = client.post("/v1/forecast", json=_payload(store_id="ZZ_9"))
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["field"] == "store_id"
    assert "CA_1" in body["error"]["details"]["supported_stores"]


def test_unknown_model_is_404(client):
    """A model this deployment does not serve does not exist."""
    response = client.post("/v1/forecast", json=_payload(model="prophet"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"


def test_unknown_model_version_is_404(client):
    """So does a version that was never registered."""
    response = client.post("/v1/forecast", json=_payload(model_version="99"))
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "MODEL_NOT_FOUND"
    assert body["error"]["details"]["available_versions"] == ["1"]


def test_malformed_body_is_rejected_without_loading_a_model(client):
    """Schema validation happens before any artifact is touched."""
    response = client.post("/v1/forecast", json={"store_id": "CA_1"})
    assert response.status_code == 422


def test_schema_errors_use_the_same_envelope_as_business_errors(client):
    """
    A malformed body and a rejected business rule share one contract.

    Both are 422; if the schema error kept FastAPI's own ``{"detail": [...]}``
    shape the frontend would need a second parser for the same status.
    """
    response = client.post("/v1/forecast", json={"store_id": "CA_1"})
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "SCENARIO_VALIDATION_ERROR"
    assert "history" in body["error"]["details"]["fields"]
    assert body["error"]["field"]
    assert "request_id" in body["error"]


def test_schema_error_details_do_not_echo_the_payload(client):
    """
    Only the location and message are reported, not the caller's input.

    Pydantic attaches the offending value; echoing it back bloats the response
    and repeats data the caller did not ask to see returned.
    """
    response = client.post(
        "/v1/forecast",
        json={**_payload(), "confidence_level": 0.42, "store_id": "SENTINEL_VALUE"},
    )
    assert response.status_code == 422
    assert "SENTINEL_VALUE" not in response.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"horizon_days": 120},
        {"confidence_level": 0.5},
        {"history": []},
    ],
)
def test_out_of_range_fields_are_422_in_the_envelope(client, overrides):
    """Each schema bound is enforced at the HTTP boundary too."""
    response = client.post("/v1/forecast", json=_payload(**overrides))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SCENARIO_VALIDATION_ERROR"


def test_error_bodies_never_leak_internals(client):
    """No URI, path, or credential may appear in a client-facing error."""
    response = client.post("/v1/forecast", json=_payload(model_version="99"))
    rendered = response.text
    for secret in ("minioadmin", "s3://", "http://mlflow", "/usr/local", "Traceback"):
        assert secret not in rendered
