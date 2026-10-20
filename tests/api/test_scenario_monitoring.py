"""Tests for scenario metrics, structured logging, and health probes."""

import json
import logging
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.core.errors import ModelNotFoundError, PredictionFailedError  # noqa: E402
from api.main import create_app  # noqa: E402
from api.observability.health import (  # noqa: E402
    CRITICAL_COMPONENTS,
    ComponentHealth,
    ComponentStatus,
    HealthReport,
    build_health_report,
    probe_database,
    probe_feature_schema,
    probe_minio,
    probe_mlflow,
)
from api.observability.scenario_logging import (  # noqa: E402
    SCHEMA_VERSION,
    EventStatus,
    ScenarioEvent,
    ScenarioLogContext,
    StageTimer,
    log_baseline_resolved,
    log_capability_check_completed,
    log_impact_calculated,
    log_overrides_applied,
    log_prediction_completed,
    log_request_received,
    log_scenario_completed,
    log_scenario_failed,
    log_scenario_rolled_back,
    log_validation_completed,
)
from api.observability.scenario_metrics import (  # noqa: E402
    CONTENT_TYPE_PROMETHEUS,
    DEFAULT_DURATION_BUCKETS,
    Counter,
    Histogram,
    ScenarioMetrics,
    get_metrics,
)
from api.services.feature_schema_service import (  # noqa: E402
    FeatureSchemaError,
    FeatureSchemaErrorCode,
)

# Reuse the route harness so monitoring is verified against real requests.
from tests.api.test_scenario_routes import (  # noqa: E402
    FakeCapabilityService,
    FakeForecastService,
    request_body,
)
from api.dependencies.scenario import (  # noqa: E402
    get_audit_service,
    get_capability_service,
    get_checkpoint_service,
    get_forecast_service,
    get_rollback_service,
)
from api.services.forecast_checkpoint_service import ForecastCheckpointService  # noqa: E402
from api.services.scenario_audit_service import ScenarioAuditService  # noqa: E402
from api.services.scenario_rollback_service import ScenarioRollbackService  # noqa: E402

CORE_LOG_FIELDS = {
    "request_id",
    "store_id",
    "model_version",
    "scenario_type",
    "status",
}


@pytest.fixture
def metrics() -> ScenarioMetrics:
    return ScenarioMetrics()


class _RecordingHandler(logging.Handler):
    """Captures emitted records for assertions."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    @property
    def events(self) -> List[Dict[str, Any]]:
        """Parsed JSON records."""
        return [json.loads(message) for message in self.messages]


@pytest.fixture
def scenario_logs():
    """
    Capture the `scenario` logger directly.

    `caplog` cannot see these: `configure_json_logging` sets
    `propagate = False` so the already-structured lines are not re-emitted
    through the root logger, which would duplicate every entry in production.
    Attaching a handler to the logger under test is the honest way to observe it.
    """
    logger = logging.getLogger("scenario")
    handler = _RecordingHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


@pytest.fixture
def context() -> ScenarioLogContext:
    return ScenarioLogContext(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
    )


# --------------------------------------------------------------------------
# Metric primitives
# --------------------------------------------------------------------------


def test_counter_accumulates_per_label_set():
    counter = Counter("demo_total", "A demo counter.")

    counter.inc(labels={"kind": "a"})
    counter.inc(2, labels={"kind": "a"})
    counter.inc(labels={"kind": "b"})

    assert counter.get({"kind": "a"}) == 3
    assert counter.get({"kind": "b"}) == 1
    assert counter.total() == 4


def test_counter_rejects_a_negative_increment():
    with pytest.raises(ValueError):
        Counter("demo_total", "A demo counter.").inc(-1)


def test_counter_renders_prometheus_text():
    counter = Counter("demo_total", "A demo counter.")
    counter.inc(labels={"kind": "a"})

    rendered = "\n".join(counter.render())

    assert "# TYPE demo_total counter" in rendered
    assert 'demo_total{kind="a"} 1.0' in rendered


def test_counter_renders_a_zero_series_when_unused():
    assert "demo_total 0" in "\n".join(Counter("demo_total", "help").render())


def test_histogram_buckets_are_cumulative():
    histogram = Histogram("demo_seconds", "A demo histogram.", buckets=(1.0, 5.0))

    for value in (0.5, 2.0, 7.0):
        histogram.observe(value)

    rendered = "\n".join(histogram.render())
    assert 'demo_seconds_bucket{le="1"} 1' in rendered
    assert 'demo_seconds_bucket{le="5"} 2' in rendered
    assert 'demo_seconds_bucket{le="+Inf"} 3' in rendered
    assert "demo_seconds_count 3" in rendered
    assert histogram.average() == pytest.approx((0.5 + 2.0 + 7.0) / 3)


def test_histogram_ignores_non_finite_observations():
    histogram = Histogram("demo_seconds", "help")

    histogram.observe(float("nan"))
    histogram.observe(float("inf"))

    assert histogram.count() == 0


def test_duration_buckets_reach_the_frontend_deadline():
    # The frontend abandons a comparison at 60s, so the top bucket must cover it.
    assert max(DEFAULT_DURATION_BUCKETS) == 60.0


# --------------------------------------------------------------------------
# Scenario metric set
# --------------------------------------------------------------------------


def test_required_metrics_are_exposed(metrics):
    rendered = metrics.render()

    for name in (
        "scenario_requests_total",
        "scenario_requests_failed_total",
        "scenario_duration_seconds",
        "scenario_rollback_total",
        "baseline_reuse_total",
        "scenario_prediction_failures_total",
    ):
        assert f"# TYPE {name}" in rendered, name


def test_requests_are_counted_by_type_and_outcome(metrics):
    metrics.record_request("promotion", "completed")
    metrics.record_request("promotion", "failed")
    metrics.record_request("traffic", "completed")

    assert metrics.requests_total.get({"scenario_type": "promotion", "status": "completed"}) == 1
    assert metrics.requests_total.get({"scenario_type": "promotion", "status": "failed"}) == 1
    assert metrics.requests_total.total() == 3


def test_prediction_failures_are_counted_separately_by_error_code(metrics):
    metrics.record_failure("promotion", "PREDICTION_FAILED", stage="prediction")
    metrics.record_failure("promotion", "SCENARIO_VALIDATION_ERROR", stage="validation")

    # Both are request failures...
    assert metrics.requests_failed_total.total() == 2
    # ...but only the model failure counts as a prediction failure, so a model
    # problem can be alerted on without client errors drowning it out.
    assert metrics.prediction_failures_total.total() == 1
    assert metrics.prediction_failures_total.get({"error_code": "PREDICTION_FAILED"}) == 1


def test_alignment_errors_count_as_prediction_failures(metrics):
    metrics.record_failure("promotion", "FORECAST_ALIGNMENT_ERROR")
    assert metrics.prediction_failures_total.total() == 1


def test_baseline_reuse_records_both_outcomes(metrics):
    metrics.record_baseline(reused=True)
    metrics.record_baseline(reused=False)

    # Both labels are recorded, so a reuse *rate* is computable.
    assert metrics.baseline_reuse_total.get({"reused": "true"}) == 1
    assert metrics.baseline_reuse_total.get({"reused": "false"}) == 1


def test_rollbacks_and_capability_checks_are_counted(metrics):
    metrics.record_rollback()
    metrics.record_capability_check("promotion", supported=False)

    assert metrics.rollback_total.get({"outcome": "succeeded"}) == 1
    assert (
        metrics.capability_checks_total.get(
            {"scenario_type": "promotion", "outcome": "unsupported"}
        )
        == 1
    )


def test_duration_is_recorded_even_when_the_block_raises(metrics):
    with pytest.raises(RuntimeError):
        with metrics.time_scenario("promotion"):
            raise RuntimeError("boom")

    # A failed scenario's latency matters as much as a successful one's.
    assert metrics.duration_seconds.count({"scenario_type": "promotion"}) == 1


def test_snapshot_totals_across_label_sets(metrics):
    metrics.record_duration("promotion", 1.5)
    metrics.record_duration("traffic", 0.5)

    snapshot = metrics.snapshot()

    assert snapshot["scenario_duration_seconds_count"] == 2
    assert snapshot["scenario_duration_seconds_sum"] == pytest.approx(2.0)


def test_reset_clears_every_metric(metrics):
    metrics.record_request("promotion", "completed")
    metrics.record_duration("promotion", 1.0)

    metrics.reset()

    assert metrics.requests_total.total() == 0
    assert metrics.duration_seconds.total_count() == 0


def test_label_values_are_escaped(metrics):
    metrics.requests_total.inc(labels={"scenario_type": 'od"d'})
    assert 'od\\"d' in metrics.render()


# --------------------------------------------------------------------------
# Structured logging
# --------------------------------------------------------------------------


def test_every_lifecycle_event_has_a_code():
    assert {event.value for event in ScenarioEvent} >= {
        "scenario.request_received",
        "scenario.validation_completed",
        "scenario.capability_check_completed",
        "scenario.baseline_created",
        "scenario.baseline_reused",
        "scenario.overrides_applied",
        "scenario.prediction_completed",
        "scenario.impact_calculated",
        "scenario.completed",
        "scenario.failed",
        "scenario.rolled_back",
    }


def test_core_fields_are_present_on_every_event(context):
    scoped = context.with_scenario_id("scn_1")
    records = [
        log_request_received(scoped, horizon_days=7, history_rows=30),
        log_validation_completed(scoped, duration_ms=3),
        log_capability_check_completed(scoped, duration_ms=5, supported=True),
        log_baseline_resolved(scoped, duration_ms=120, reused=False, baseline_forecast_id="bf_1"),
        log_overrides_applied(scoped, duration_ms=8, applied_features=["is_promotion"]),
        log_prediction_completed(scoped, duration_ms=900, horizon_days=7),
        log_impact_calculated(scoped, duration_ms=4, total_difference=150.0),
        log_scenario_completed(scoped, duration_ms=1100),
        log_scenario_rolled_back(scoped, reason="withdrawn"),
    ]

    for record in records:
        assert CORE_LOG_FIELDS <= set(record), record["event"]
        assert record["scenario_id"] == "scn_1"
        assert record["schema_version"] == SCHEMA_VERSION
        assert record["timestamp"].endswith("Z")


def test_events_carry_a_duration(context):
    assert log_prediction_completed(context, duration_ms=900, horizon_days=7)[
        "duration_ms"
    ] == 900
    assert log_scenario_completed(context, duration_ms=1100)["duration_ms"] == 1100


def test_failure_events_carry_the_taxonomy_error_code(context):
    record = log_scenario_failed(
        context, ModelNotFoundError(field="model_version"), duration_ms=12, stage="baseline"
    )

    # The same code the client received, which is what makes a support request
    # traceable to a log line.
    assert record["error_code"] == "MODEL_NOT_FOUND"
    assert record["http_status"] == 404
    assert record["stage"] == "baseline"
    assert record["status"] == EventStatus.FAILED.value


def test_failure_event_maps_a_service_error(context):
    record = log_scenario_failed(
        context,
        FeatureSchemaError(
            FeatureSchemaErrorCode.FEATURE_SCHEMA_MISSING, "no schema", field="model_version"
        ),
    )

    assert record["error_code"] == "FEATURE_SCHEMA_MISSING"


def test_log_messages_are_sanitised(context):
    record = log_scenario_failed(
        context,
        PredictionFailedError(
            "failed reading /usr/local/airflow/models/x.pkl via postgresql://u:p@db:5432/x"
        ),
    )

    assert "/usr/local/airflow" not in record["message"]
    assert "postgresql://" not in record["message"]
    assert "[redacted]" in record["message"]


def test_request_payloads_are_never_logged(context):
    record = log_request_received(context, horizon_days=7, history_rows=30)

    # Only the shape of the caller's history, never the series itself.
    assert record["history_rows"] == 30
    assert "history" not in record
    assert "request_payload" not in record


def test_partial_overrides_are_flagged(context):
    record = log_overrides_applied(
        context,
        duration_ms=5,
        applied_features=["is_promotion"],
        unsupported_features=["promotion_intensity"],
        modified_days=3,
    )

    assert record["partial"] is True
    assert record["status"] == EventStatus.SKIPPED.value
    assert record["unsupported_features"] == ["promotion_intensity"]


def test_baseline_reuse_selects_a_distinct_event(context):
    created = log_baseline_resolved(context, duration_ms=1, reused=False)
    reused = log_baseline_resolved(context, duration_ms=1, reused=True)

    assert created["event"] == ScenarioEvent.BASELINE_CREATED.value
    assert reused["event"] == ScenarioEvent.BASELINE_REUSED.value


def test_events_are_emitted_as_single_json_lines(context, scenario_logs):
    log_scenario_completed(context.with_scenario_id("scn_1"), duration_ms=10)

    assert len(scenario_logs.messages) == 1
    parsed = scenario_logs.events[0]
    assert parsed["event"] == "scenario.completed"
    assert parsed["scenario_id"] == "scn_1"


def test_context_copies_do_not_mutate_the_original(context):
    scoped = context.with_scenario_id("scn_1").with_baseline("bf_1")

    assert context.scenario_id is None
    assert scoped.scenario_id == "scn_1"
    assert scoped.baseline_forecast_id == "bf_1"


def test_stage_timer_measures_and_resets():
    timer = StageTimer()
    assert timer.elapsed_ms() >= 0
    timer.reset()
    assert timer.elapsed_ms() >= 0


# --------------------------------------------------------------------------
# Health probes
# --------------------------------------------------------------------------


def test_unconfigured_dependencies_report_not_configured(monkeypatch):
    for name in ("SCENARIO_DATABASE_URL", "DATABASE_URL", "MLFLOW_TRACKING_URI", "MLFLOW_S3_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)

    # "Not configured" is a different fact from "unreachable", and the probes
    # must not conflate them.
    assert probe_database().status is ComponentStatus.NOT_CONFIGURED
    assert probe_mlflow().status is ComponentStatus.NOT_CONFIGURED
    assert probe_minio().status is ComponentStatus.NOT_CONFIGURED
    assert probe_feature_schema().status is ComponentStatus.NOT_CONFIGURED


def test_database_probe_succeeds_against_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("SCENARIO_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'health.db'}")

    result = probe_database()

    assert result.status is ComponentStatus.OK
    assert result.latency_ms is not None


def test_unreachable_mlflow_is_unavailable(monkeypatch):
    # Port 1 is reserved and never listening.
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:1")

    result = probe_mlflow()

    assert result.status is ComponentStatus.UNAVAILABLE
    assert result.latency_ms is not None


def test_probe_details_never_leak_infrastructure(monkeypatch):
    monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", "http://minio-internal.svc:9000")

    detail = probe_minio().to_dict().get("detail", "")

    assert "minio-internal.svc" not in detail


def test_report_is_degraded_when_a_non_critical_dependency_is_down():
    report = HealthReport(
        ComponentStatus.DEGRADED,
        "0.1.0",
        [
            ComponentHealth("api", ComponentStatus.OK),
            ComponentHealth("database", ComponentStatus.OK),
            ComponentHealth("feature_schema", ComponentStatus.DEGRADED),
        ],
    )

    # A missing feature schema must not take the service out of rotation.
    assert report.is_ready is True
    assert report.to_dict()["status"] == "degraded"


def test_report_is_not_ready_when_a_critical_dependency_is_down():
    report = HealthReport(
        ComponentStatus.UNAVAILABLE,
        "0.1.0",
        [
            ComponentHealth("api", ComponentStatus.OK),
            ComponentHealth("database", ComponentStatus.UNAVAILABLE),
        ],
    )

    assert report.is_ready is False
    assert "database" in CRITICAL_COMPONENTS


def test_shallow_report_touches_no_dependency():
    report = build_health_report("0.1.0", deep=False)

    assert report.status is ComponentStatus.OK
    assert [item.name for item in report.components] == ["api"]


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def test_health_reports_every_required_component(client):
    body = client.get("/health").json()

    assert body["success"] is True
    assert set(body["components"]) == {
        "api",
        "database",
        "mlflow",
        "minio",
        "feature_schema",
    }
    assert body["components"]["api"]["status"] == "ok"


def test_health_stays_200_even_when_dependencies_are_down(client):
    response = client.get("/health")

    # Liveness: the process is serving, so an orchestrator must not restart it
    # because an upstream is unavailable.
    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded", "unavailable"}


def test_readiness_fails_closed_without_a_database(client):
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_shallow_health_is_cheap(client):
    body = client.get("/health", params={"deep": "false"}).json()

    assert body["status"] == "ok"
    assert set(body["components"]) == {"api"}


def test_metrics_endpoint_renders_prometheus_text(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert CONTENT_TYPE_PROMETHEUS.startswith("text/plain")
    assert "# TYPE scenario_requests_total counter" in response.text


# --------------------------------------------------------------------------
# Instrumentation through a real request
# --------------------------------------------------------------------------


@pytest.fixture
def instrumented(tmp_path):
    """An app with fake services but the real metrics and logging wiring."""
    url = f"sqlite+pysqlite:///{tmp_path / 'monitoring.db'}"
    audit = ScenarioAuditService(database_url=url, create_tables=True)
    checkpoints = ForecastCheckpointService(database_url=url, create_tables=True)

    class Context:
        def __init__(self) -> None:
            self.capability_service = FakeCapabilityService()
            self.forecast_service = FakeForecastService()
            self.app = create_app()
            self.app.dependency_overrides[get_capability_service] = (
                lambda: self.capability_service
            )
            self.app.dependency_overrides[get_forecast_service] = (
                lambda: self.forecast_service
            )
            self.app.dependency_overrides[get_audit_service] = lambda: audit
            self.app.dependency_overrides[get_checkpoint_service] = lambda: checkpoints
            self.app.dependency_overrides[get_rollback_service] = (
                lambda: ScenarioRollbackService(
                    audit_service=audit, checkpoint_service=checkpoints
                )
            )
            self.client = TestClient(self.app, raise_server_exceptions=False)

    get_metrics().reset()
    return Context()


def test_a_successful_comparison_updates_metrics(instrumented):
    instrumented.client.post("/v1/scenario/compare", json=request_body())

    metrics = get_metrics()
    assert (
        metrics.requests_total.get({"scenario_type": "promotion", "status": "completed"})
        == 1
    )
    assert metrics.duration_seconds.count({"scenario_type": "promotion"}) == 1
    assert metrics.capability_checks_total.total() == 1
    assert metrics.baseline_reuse_total.total() == 1
    assert metrics.requests_failed_total.total() == 0


def test_a_failed_comparison_updates_failure_metrics(instrumented):
    instrumented.forecast_service.error = PredictionFailedError()

    instrumented.client.post("/v1/scenario/compare", json=request_body())

    metrics = get_metrics()
    assert metrics.requests_failed_total.get(
        {"scenario_type": "promotion", "error_code": "PREDICTION_FAILED"}
    ) == 1
    assert metrics.prediction_failures_total.total() == 1
    # Latency is recorded for failures too.
    assert metrics.duration_seconds.count({"scenario_type": "promotion"}) == 1


def test_an_unsupported_scenario_is_counted_at_the_capability_check(instrumented):
    instrumented.capability_service.supported = False

    instrumented.client.post("/v1/scenario/compare", json=request_body())

    metrics = get_metrics()
    assert (
        metrics.capability_checks_total.get(
            {"scenario_type": "promotion", "outcome": "unsupported"}
        )
        == 1
    )
    assert metrics.requests_failed_total.total() == 1


def test_a_rollback_is_counted(instrumented):
    created = instrumented.client.post("/v1/scenario/compare", json=request_body()).json()

    instrumented.client.post(
        f"/v1/scenario/{created['scenario_id']}/rollback",
        json={"reason": "assumptions were wrong"},
    )

    assert get_metrics().rollback_total.get({"outcome": "succeeded"}) == 1


def test_the_pipeline_emits_its_lifecycle_events(instrumented, scenario_logs):
    instrumented.client.post("/v1/scenario/compare", json=request_body())

    events = [event["event"] for event in scenario_logs.events]

    assert "scenario.request_received" in events
    assert "scenario.validation_completed" in events
    assert "scenario.capability_check_completed" in events
    assert "scenario.completed" in events


def test_every_emitted_event_carries_the_correlation_fields(instrumented, scenario_logs):
    instrumented.client.post("/v1/scenario/compare", json=request_body())

    assert scenario_logs.events
    for parsed in scenario_logs.events:
        assert CORE_LOG_FIELDS <= set(parsed), parsed["event"]
        assert parsed["request_id"].startswith("req_")


def test_a_failure_emits_a_failed_event_with_its_code(instrumented, scenario_logs):
    instrumented.forecast_service.error = PredictionFailedError()

    instrumented.client.post("/v1/scenario/compare", json=request_body())

    failures = [
        event for event in scenario_logs.events if event["event"] == "scenario.failed"
    ]

    assert len(failures) == 1
    assert failures[0]["error_code"] == "PREDICTION_FAILED"
    assert failures[0]["duration_ms"] >= 0


def test_the_forecast_service_receives_a_log_context(instrumented):
    instrumented.client.post("/v1/scenario/compare", json=request_body())

    context = instrumented.forecast_service.log_contexts[0]
    assert context is not None
    assert context.request_id.startswith("req_")
    assert context.store_id == "store_001"
