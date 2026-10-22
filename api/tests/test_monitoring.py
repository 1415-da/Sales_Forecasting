"""
Monitoring: metric arithmetic, drift, alert lifecycle, and the no-data contract.

The metric functions are checked against hand-computed values rather than
golden files, so a change in behaviour shows up as a specific wrong number
instead of a diff nobody reads.

The no-data contract gets as much attention as the arithmetic. A monitoring
page that renders a plausible zero when nothing has been scored is worse than
one that renders nothing, because the zero is believed and then acted on.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, timedelta

import pytest

from api.db.models.monitoring import AlertSeverity, AlertStatus, AlertType, DriftStatus
from api.db.repositories.monitoring_repository import (
    ActualRepository,
    ForecastRepository,
    MonitoringRepository,
)
from api.db.base import utcnow
from api.db.session import session_scope
from api.services.monitoring_metrics import (
    MIN_DRIFT_SAMPLE,
    DriftMethod,
    compute_accuracy,
    evaluate_drift,
    kolmogorov_smirnov,
    population_stability_index,
    relative_change,
)
from api.services.monitoring_service import (
    NO_DATA_MESSAGE,
    WAPE_CRITICAL,
    WAPE_WARNING,
    MonitoringService,
    alert_fingerprint,
)

# ---------------------------------------------------------------------------
# Accuracy arithmetic
# ---------------------------------------------------------------------------


def pair(predicted, actual, lower=None, upper=None):
    """One forecast/actual pair."""
    row = {"predicted": predicted, "actual": actual}
    if lower is not None:
        row["lower_bound"] = lower
    if upper is not None:
        row["upper_bound"] = upper
    return row


def test_accuracy_matches_hand_computed_values():
    """
    Checked against values worked out by hand.

    predicted [110, 90, 105] against actual [100, 100, 100]:
      errors     +10, -10, +5
      MAE        25/3   = 8.3333
      RMSE       sqrt(225/3) = 8.6603
      bias       5/3    = 1.6667
      WAPE       25/300 = 0.08333
    """
    result = compute_accuracy([pair(110, 100), pair(90, 100), pair(105, 100)])

    assert result.sample_size == 3
    assert result.mae == pytest.approx(25 / 3)
    assert result.rmse == pytest.approx(math.sqrt(225 / 3))
    assert result.bias == pytest.approx(5 / 3)
    assert result.wape == pytest.approx(25 / 300)
    assert result.mape == pytest.approx(25 / 300)


def test_an_empty_window_is_empty_not_zero():
    """
    "Nothing scored" and "no error" must not be the same value.

    A zero here would be indistinguishable from a perfect window once stored.
    """
    result = compute_accuracy([])
    assert result.is_empty is True
    assert result.sample_size == 0
    assert result.wape is None
    assert result.mae is None
    # Nothing is offered for storage, so no misleading row can be written.
    assert result.as_metric_map() == {}


def test_wape_is_undefined_when_nothing_sold():
    """
    A window of all-zero actuals has no volume to express error against.

    MAE is still reported -- the absolute error is real -- but a percentage of
    zero is not a number.
    """
    result = compute_accuracy([pair(10, 0), pair(5, 0)])
    assert result.wape is None
    assert result.mape is None
    assert result.mae == pytest.approx(7.5)


def test_mape_skips_zero_actuals_rather_than_becoming_infinite():
    """
    Dividing by a zero actual is what makes MAPE unusable on this data.

    Only non-zero days contribute, which is why WAPE is the headline metric
    and MAPE is reported alongside it rather than instead of it.
    """
    result = compute_accuracy([pair(110, 100), pair(50, 0)])
    assert result.mape == pytest.approx(0.10)
    assert math.isfinite(result.mape)


def test_interval_coverage_measures_the_actual_against_the_band():
    """
    Coverage asks whether the outcome fell inside the interval.

    It is the only metric that tests the uncertainty band rather than the point
    forecast, which is what makes an over-confident interval detectable.
    """
    result = compute_accuracy(
        [
            pair(100, 100, 90, 110),   # inside
            pair(100, 150, 90, 110),   # outside
            pair(100, 95, 90, 110),    # inside
            pair(100, 80, 90, 110),    # outside
        ]
    )
    assert result.interval_coverage == pytest.approx(0.5)


def test_coverage_is_none_without_bounds():
    """A point forecast with no interval has no coverage to report."""
    assert compute_accuracy([pair(100, 100)]).interval_coverage is None


def test_bias_distinguishes_one_sided_error_from_noise():
    """
    Two series with the same MAE and very different problems.

    Only the one-directional one is fixable by recalibration, which is why
    bias is reported separately.
    """
    one_sided = compute_accuracy([pair(110, 100), pair(110, 100)])
    alternating = compute_accuracy([pair(110, 100), pair(90, 100)])

    assert one_sided.mae == pytest.approx(alternating.mae)
    assert one_sided.bias == pytest.approx(10.0)
    assert alternating.bias == pytest.approx(0.0)


def test_r2_is_none_for_a_constant_series():
    """With no variance in the actuals there is nothing for R² to explain."""
    assert compute_accuracy([pair(100, 100), pair(105, 100)]).r2 is None


def test_non_finite_pairs_are_discarded():
    """A NaN must not poison every metric in the window."""
    result = compute_accuracy(
        [pair(110, 100), pair(float("nan"), 100), pair(90, float("inf"))]
    )
    assert result.sample_size == 1


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def test_psi_is_near_zero_for_the_same_distribution():
    """An unchanged distribution scores as stable."""
    import random

    rng = random.Random(11)
    reference = [rng.gauss(0, 1) for _ in range(400)]
    current = [rng.gauss(0, 1) for _ in range(400)]
    score, n_ref, n_cur = population_stability_index(reference, current)

    assert n_ref == 400 and n_cur == 400
    assert score < 0.1


def test_psi_is_large_for_a_shifted_distribution():
    """A two-sigma shift is well past the drift threshold."""
    import random

    rng = random.Random(11)
    reference = [rng.gauss(0, 1) for _ in range(400)]
    current = [rng.gauss(2, 1) for _ in range(400)]
    score, _, _ = population_stability_index(reference, current)
    assert score > 0.25


def test_psi_handles_a_constant_reference():
    """
    A reference with no spread cannot be bucketed by quantile.

    Drift then reduces to a binary question -- does the current sample still
    sit on that value -- rather than an error.
    """
    score, _, _ = population_stability_index([5.0] * 50, [5.0] * 50)
    assert score == pytest.approx(0.0)
    score, _, _ = population_stability_index([5.0] * 50, [9.0] * 50)
    assert score == pytest.approx(1.0)


def test_ks_is_bounded_and_directionless():
    """The KS statistic is a CDF gap, so it lies in [0, 1] either way round."""
    reference = list(range(100))
    current = [value + 200 for value in range(100)]
    forward, _, _ = kolmogorov_smirnov(reference, current)
    backward, _, _ = kolmogorov_smirnov(current, reference)

    assert 0.0 <= forward <= 1.0
    assert forward == pytest.approx(backward)
    assert forward == pytest.approx(1.0)


def test_an_identical_sample_has_no_ks_gap():
    """Identical samples have no CDF separation."""
    sample = [float(value) for value in range(50)]
    score, _, _ = kolmogorov_smirnov(sample, sample)
    assert score == pytest.approx(0.0)


def test_a_small_sample_is_flagged_rather_than_scored():
    """
    A PSI from a dozen points is noise.

    Alerting on it would train people to ignore drift alerts, so it is
    reported as insufficient and classified stable.
    """
    result = evaluate_drift(
        feature_name="f",
        reference=[1.0, 2.0, 3.0],
        current=[90.0, 91.0, 92.0],
        method=DriftMethod.PSI,
    )
    assert result.insufficient_data is True
    assert result.status == DriftStatus.STABLE.value
    assert result.reference_size < MIN_DRIFT_SAMPLE


def test_drift_classification_uses_each_method_own_thresholds():
    """PSI and KS are on different scales and must not share a threshold."""
    import random

    rng = random.Random(3)
    reference = [rng.gauss(0, 1) for _ in range(200)]
    shifted = [rng.gauss(3, 1) for _ in range(200)]

    psi = evaluate_drift(
        feature_name="f", reference=reference, current=shifted, method=DriftMethod.PSI
    )
    ks = evaluate_drift(
        feature_name="f", reference=reference, current=shifted, method=DriftMethod.KS
    )
    assert psi.status == DriftStatus.DRIFT_DETECTED.value
    assert ks.status == DriftStatus.DRIFT_DETECTED.value
    assert psi.threshold != ks.threshold


def test_relative_change_refuses_to_divide_by_zero():
    """
    A percentage change from zero is not a number.

    Reporting it as infinite or as 100% would both be wrong, so it is None.
    """
    assert relative_change(0.12, 0.10) == pytest.approx(0.2)
    assert relative_change(0.12, 0) is None
    assert relative_change(None, 0.1) is None
    assert relative_change(0.1, None) is None


# ---------------------------------------------------------------------------
# Storage and the alert lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    """A ``MonitoringRepository`` over a committed session."""

    class Factory:
        def __enter__(self):
            self._scope = session_scope()
            self.session = self._scope.__enter__()
            return MonitoringRepository(self.session)

        def __exit__(self, *exc):
            return self._scope.__exit__(*exc)

    return Factory


def test_re_evaluating_a_date_corrects_it_rather_than_duplicating(store):
    """
    The DAG is re-runnable.

    A second row for the same window would double-count the date in every
    trend chart.
    """
    version = f"v-{uuid.uuid4().hex[:8]}"
    today = date(2026, 5, 1)
    window = (date(2026, 4, 4), today)

    with store() as repository:
        first = repository.upsert_metric(
            model_version=version, metric_name="wape", metric_value=0.2,
            evaluation_date=today, window_start=window[0], window_end=window[1],
        )
        first_id = first.id
    with store() as repository:
        second = repository.upsert_metric(
            model_version=version, metric_name="wape", metric_value=0.15,
            evaluation_date=today, window_start=window[0], window_end=window[1],
        )
        assert second.id == first_id
        assert second.metric_value == pytest.approx(0.15)


def test_an_actual_is_corrected_not_duplicated():
    """
    A revised store-day is an update.

    Inserting it again would double-count the day in every accuracy metric.
    """
    store_id = f"S-{uuid.uuid4().hex[:6]}"
    day = date(2026, 4, 10)
    with session_scope() as session:
        inserted, updated = ActualRepository(session).upsert_many(
            [{"store_id": store_id, "date": day, "actual_sales": 100.0}]
        )
        assert (inserted, updated) == (1, 0)
    with session_scope() as session:
        inserted, updated = ActualRepository(session).upsert_many(
            [{"store_id": store_id, "date": day, "actual_sales": 125.0}]
        )
        assert (inserted, updated) == (0, 1)
    with session_scope() as session:
        series = ActualRepository(session).series(
            store_id=store_id, start=day, end=day
        )
        assert series == [(day, 125.0)]


def test_an_ongoing_alert_is_refreshed_not_refiled(store):
    """
    One row per condition, with a rising occurrence count.

    Without deduplication a daily DAG would file the same problem thirty times
    and the panel would become unreadable.
    """
    fingerprint = alert_fingerprint(
        alert_type=AlertType.ACCURACY_DEGRADATION.value,
        model_version=f"v-{uuid.uuid4().hex[:8]}",
    )
    with store() as repository:
        first, created = repository.upsert_alert(
            fingerprint=fingerprint, alert_type=AlertType.ACCURACY_DEGRADATION.value,
            severity=AlertSeverity.WARNING.value, title="WAPE 22%", message="m",
        )
        assert created is True
        assert first.occurrence_count == 1
        alert_id = first.id
    with store() as repository:
        second, created = repository.upsert_alert(
            fingerprint=fingerprint, alert_type=AlertType.ACCURACY_DEGRADATION.value,
            severity=AlertSeverity.CRITICAL.value, title="WAPE 38%", message="m",
        )
        assert created is False
        assert second.id == alert_id
        assert second.occurrence_count == 2
        # The severity follows the current reading, not the first one.
        assert second.severity == AlertSeverity.CRITICAL.value


def test_a_recurrence_after_resolution_opens_a_new_alert(store):
    """
    A problem returning after a human closed it is new information.

    Reusing the resolved row would hide that it came back.
    """
    fingerprint = alert_fingerprint(
        alert_type=AlertType.STALE_DATA.value,
        model_version=f"v-{uuid.uuid4().hex[:8]}",
    )
    with store() as repository:
        first, _ = repository.upsert_alert(
            fingerprint=fingerprint, alert_type=AlertType.STALE_DATA.value,
            severity=AlertSeverity.WARNING.value, title="stale", message="m",
        )
        repository.resolve_alert(first, user_id=None, note="fixed")
        first_id = first.id
    with store() as repository:
        second, created = repository.upsert_alert(
            fingerprint=fingerprint, alert_type=AlertType.STALE_DATA.value,
            severity=AlertSeverity.WARNING.value, title="stale again", message="m",
        )
        assert created is True
        assert second.id != first_id


def test_acknowledge_and_resolve_are_idempotent(store):
    """Repeating either must not disturb the recorded timestamps."""
    with store() as repository:
        alert, _ = repository.upsert_alert(
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
            alert_type=AlertType.BIAS.value,
            severity=AlertSeverity.WARNING.value, title="t", message="m",
        )
        repository.acknowledge_alert(alert, user_id=None)
        acknowledged_at = alert.acknowledged_at
        repository.acknowledge_alert(alert, user_id=None)
        assert alert.acknowledged_at == acknowledged_at
        assert alert.status == AlertStatus.ACKNOWLEDGED.value

        repository.resolve_alert(alert, user_id=None, note="done")
        resolved_at = alert.resolved_at
        repository.resolve_alert(alert, user_id=None, note="again")
        assert alert.resolved_at == resolved_at
        assert alert.resolution_note == "done"


def test_an_alert_whose_condition_stopped_recurring_is_auto_resolved(store):
    """
    Otherwise a fixed problem stays on the dashboard forever.

    A panel full of stale alerts is one people stop reading.
    """
    from api.db.base import utcnow

    with store() as repository:
        alert, _ = repository.upsert_alert(
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
            alert_type=AlertType.FEATURE_DRIFT.value,
            severity=AlertSeverity.WARNING.value, title="t", message="m",
        )
        alert.last_seen_at = utcnow() - timedelta(days=45)
        repository.session.flush()

        assert repository.auto_resolve_stale(older_than_days=30) >= 1
        assert alert.status == AlertStatus.RESOLVED.value
        assert "has not recurred" in (alert.resolution_note or "")


# ---------------------------------------------------------------------------
# The no-data contract
# ---------------------------------------------------------------------------


def test_a_platform_with_no_outcomes_reports_no_data_not_healthy():
    """
    The single most important behaviour on this page.

    A system that has measured nothing is not known to be working, and a green
    badge over an empty table is the one mistake a monitoring dashboard cannot
    afford.
    """
    with session_scope() as session:
        service = MonitoringService(session)
        accuracy = service.accuracy(
            window_days=28,
            model_version=f"never-used-{uuid.uuid4().hex[:8]}",
            reference_date=date(2030, 1, 1),
        )
        assert accuracy.has_data is False

        recommendation = service.retraining_recommendation(
            accuracy=accuracy, drift_rows=[], production_model=None
        )
        assert recommendation.recommendation == "no_data"
        assert "cannot be assessed" in recommendation.reason


def test_nothing_is_written_for_an_unscoreable_window():
    """
    No metric row rather than a row of zeros.

    Once stored, a zero is indistinguishable from a genuinely perfect window.
    """
    with session_scope() as session:
        service = MonitoringService(session)
        accuracy = service.accuracy(
            window_days=28,
            model_version=f"never-used-{uuid.uuid4().hex[:8]}",
            reference_date=date(2030, 1, 1),
        )
        written = service.persist_accuracy(
            model_version="never-used",
            accuracy=accuracy,
            evaluation_date=date(2030, 1, 1),
        )
        assert written == 0


def test_drift_is_reported_even_when_accuracy_cannot_be_computed():
    """
    Drift comes from stored rows and can be known without a scorable window.

    Reporting a flat "no data" while the drift table shows detected drift would
    have the summary contradicting the panel below it.
    """
    with session_scope() as session:
        service = MonitoringService(session)
        accuracy = service.accuracy(
            window_days=28,
            model_version=f"never-used-{uuid.uuid4().hex[:8]}",
            reference_date=date(2030, 1, 1),
        )
        recommendation = service.retraining_recommendation(
            accuracy=accuracy,
            drift_rows=[
                {"feature_name": "f1", "status": DriftStatus.DRIFT_DETECTED.value},
                {"feature_name": "f2", "status": DriftStatus.DRIFT_DETECTED.value},
            ],
            production_model=None,
        )
        assert recommendation.recommendation == "investigate"
        assert "drifted" in recommendation.reason
        assert recommendation.drift_features == 2


def test_the_no_data_message_is_the_documented_sentence():
    """The wording is part of the contract the frontend renders."""
    assert NO_DATA_MESSAGE == (
        "Monitoring will populate after forecasts and actual outcomes are recorded."
    )


# ---------------------------------------------------------------------------
# Alert thresholds
# ---------------------------------------------------------------------------


def test_thresholds_are_ordered_so_critical_implies_warning():
    """An inverted pair would make the critical band unreachable."""
    assert 0 < WAPE_WARNING < WAPE_CRITICAL < 1


def test_a_fingerprint_ignores_the_measured_value():
    """
    An accuracy problem worsening is the same condition, not a new one.

    A value-sensitive fingerprint would file a fresh alert every day.
    """
    first = alert_fingerprint(alert_type="accuracy_degradation", model_version="1")
    second = alert_fingerprint(alert_type="accuracy_degradation", model_version="1")
    other = alert_fingerprint(alert_type="accuracy_degradation", model_version="2")

    assert first == second
    assert first != other


# ---------------------------------------------------------------------------
# Reference date
# ---------------------------------------------------------------------------


class _FixedActuals:
    """Stands in for ``ActualRepository`` with a known newest outcome."""

    def __init__(self, latest):
        self._latest = latest

    def latest_date(self, *, store_id=None):
        return self._latest


def _clock_service(latest):
    with session_scope() as session:
        service = MonitoringService(session)
    service.actuals = _FixedActuals(latest)
    return service


def test_a_historical_dataset_anchors_windows_at_its_last_outcome(monkeypatch):
    """
    Replayed data ends in the past.

    Anchored at the wall clock, every window would end years after the last
    outcome and the page would say "no data" forever.
    """
    monkeypatch.setenv("MONITORING_REFERENCE_DATE", "latest_actual")
    service = _clock_service(date(2016, 4, 24))
    assert service.reference_date() == date(2016, 4, 24)
    assert service.reference_date(date(2016, 3, 1)) == date(2016, 3, 1)


def test_a_data_clock_without_outcomes_falls_back_to_today(monkeypatch):
    monkeypatch.setenv("MONITORING_REFERENCE_DATE", "latest_actual")
    assert _clock_service(None).reference_date() == utcnow().date()


def test_the_default_clock_is_today_and_a_date_pins_it(monkeypatch):
    service = _clock_service(date(2016, 4, 24))
    monkeypatch.delenv("MONITORING_REFERENCE_DATE", raising=False)
    assert service.reference_date() == utcnow().date()
    monkeypatch.setenv("MONITORING_REFERENCE_DATE", "2016-02-01")
    assert service.reference_date() == date(2016, 2, 1)
    monkeypatch.setenv("MONITORING_REFERENCE_DATE", "not-a-date")
    assert service.reference_date() == utcnow().date()
