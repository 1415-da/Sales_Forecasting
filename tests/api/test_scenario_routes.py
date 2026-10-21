"""Tests for the Scenario Planner HTTP routes."""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.core.errors import (  # noqa: E402
    MLflowUnavailableError,
    ModelNotFoundError,
    PredictionFailedError,
)
from api.dependencies.scenario import (  # noqa: E402
    get_audit_service,
    get_capability_service,
    get_checkpoint_service,
    get_forecast_service,
    get_rollback_service,
)
from api.main import create_app  # noqa: E402
from api.middleware.error_handler import REQUEST_ID_HEADER  # noqa: E402
from api.schemas.baseline_forecast import (  # noqa: E402
    BaselineDailyForecast,
    ModelStage,
    UncertaintyMethod,
)
from api.schemas.scenario import ScenarioType  # noqa: E402
from api.schemas.scenario_capabilities import (  # noqa: E402
    RequirementMode,
    ScenarioCapabilitiesResponse,
    ScenarioCapabilityStatus,
)
from api.schemas.scenario_comparison import (  # noqa: E402
    SCENARIO_LIMITATIONS,
    ForecastSeriesSummary,
    ForecastType,
    ScenarioComparisonResponse,
    ScenarioImpact,
    new_scenario_forecast_id,
    new_scenario_id,
)
from api.services.feature_schema_service import (  # noqa: E402
    FeatureSchemaError,
    FeatureSchemaErrorCode,
)
from api.services.forecast_checkpoint_service import ForecastCheckpointService  # noqa: E402
from api.services.scenario_audit_service import (  # noqa: E402
    ScenarioAuditService,
    ScenarioAuditStatus,
)
from api.services.scenario_feature_service import (  # noqa: E402
    ScenarioNotSupportedError as OverrideNotSupportedError,
)
from api.services.scenario_impact_service import ScenarioImpactService  # noqa: E402
from api.services.scenario_rollback_service import ScenarioRollbackService  # noqa: E402
from api.services.scenario_validation import ScenarioValidationService  # noqa: E402

FORECAST_START = date(2026, 2, 1)
HORIZON_DAYS = 7
HISTORY_START = date(2026, 1, 1)
HISTORY_DAYS = 30
SCENARIO_ID = "scn_fixed_for_tests"
BASELINE_ID = "bf_fixed_for_tests"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


def build_capabilities(
    supported: bool = True, model_version: str = "14"
) -> ScenarioCapabilitiesResponse:
    """Capability payload with every scenario type set to ``supported``."""
    return ScenarioCapabilitiesResponse(
        model_version=model_version,
        feature_count=5,
        features=["is_promotion", "customer_traffic", "is_closed", "is_holiday", "price"],
        capabilities=[
            ScenarioCapabilityStatus(
                scenario_type=scenario_type,
                supported=supported,
                required_features=["is_promotion"],
                available_features=["is_promotion"] if supported else [],
                missing_features=[] if supported else ["is_promotion"],
                requirement_mode=RequirementMode.ANY,
                reason=None
                if supported
                else "Model version 14 was not trained with any of: is_promotion.",
            )
            for scenario_type in ScenarioType
        ],
    )


class FakeCapabilityService:
    """Returns canned capabilities, or raises."""

    def __init__(self, supported: bool = True, error: Optional[Exception] = None) -> None:
        self.supported = supported
        self.error = error
        self.calls: List[str] = []

    def get_capabilities(self, model_version, requested_overrides=None):
        self.calls.append(model_version)
        if self.error:
            raise self.error
        return build_capabilities(supported=self.supported)


def build_comparison(
    scenario_id: str = SCENARIO_ID,
    baseline_forecast_id: str = BASELINE_ID,
    total_difference: float = 150.0,
    baseline_reused: bool = False,
) -> ScenarioComparisonResponse:
    """A minimal but complete comparison response."""
    baseline = [
        BaselineDailyForecast(
            date=FORECAST_START + timedelta(days=offset),
            predicted_sales=100.0,
            lower_bound=90.0,
            upper_bound=110.0,
            uncertainty_method=UncertaintyMethod.RESIDUAL_BASED,
        )
        for offset in range(HORIZON_DAYS)
    ]
    scenario = [
        row.model_copy(
            update={
                "predicted_sales": row.predicted_sales
                + (total_difference / HORIZON_DAYS)
            }
        )
        for row in baseline
    ]
    impact = ScenarioImpactService().compute_impact(baseline, scenario)

    return ScenarioComparisonResponse(
        scenario_id=scenario_id,
        baseline_forecast_id=baseline_forecast_id,
        scenario_forecast_id=new_scenario_forecast_id(),
        model_version="14",
        model_stage=ModelStage.PRODUCTION.value,
        scenario_type=ScenarioType.PROMOTION,
        forecast_type=ForecastType.PREDICTIVE_WHAT_IF,
        baseline=ForecastSeriesSummary(
            forecast_id=baseline_forecast_id,
            horizon_days=HORIZON_DAYS,
            start_date=FORECAST_START,
            end_date=FORECAST_START + timedelta(days=HORIZON_DAYS - 1),
            total_forecast=700.0,
        ),
        scenario=ForecastSeriesSummary(
            horizon_days=HORIZON_DAYS,
            start_date=FORECAST_START,
            end_date=FORECAST_START + timedelta(days=HORIZON_DAYS - 1),
            total_forecast=700.0 + total_difference,
        ),
        impact=impact,
        daily_forecasts=list(impact.daily_impacts),
        modified_features=["is_promotion"],
        store_id="store_001",
        baseline_reused=baseline_reused,
        limitations=list(SCENARIO_LIMITATIONS),
    )


class FakeForecastService:
    """Returns a canned comparison, or raises."""

    def __init__(
        self,
        comparison: Optional[ScenarioComparisonResponse] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.comparison = comparison
        self.error = error
        self.calls: List[object] = []
        self.log_contexts: List[object] = []

    def create_comparison(self, request, log_context=None):
        # Mirrors the real service's signature: the route passes a log context
        # so each pipeline stage can emit a structured event.
        self.log_contexts.append(log_context)
        self.calls.append(request)
        if self.error:
            raise self.error
        if self.comparison is not None:
            return self.comparison
        return build_comparison(
            baseline_forecast_id=request.baseline_forecast_id or BASELINE_ID,
            baseline_reused=bool(request.baseline_forecast_id),
        )


# --------------------------------------------------------------------------
# App fixture
# --------------------------------------------------------------------------


@pytest.fixture
def database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'routes.db'}"


@pytest.fixture
def audit_service(database_url) -> ScenarioAuditService:
    return ScenarioAuditService(database_url=database_url, create_tables=True)


@pytest.fixture
def checkpoint_service(database_url) -> ForecastCheckpointService:
    return ForecastCheckpointService(database_url=database_url, create_tables=True)


@pytest.fixture
def context(audit_service, checkpoint_service):
    """App plus the fakes wired into it, so tests can assert on both."""

    class Context:
        def __init__(self) -> None:
            self.capability_service = FakeCapabilityService()
            self.forecast_service = FakeForecastService()
            self.audit_service = audit_service
            self.checkpoint_service = checkpoint_service
            self.rollback_service = ScenarioRollbackService(
                audit_service=audit_service, checkpoint_service=checkpoint_service
            )
            self.app = create_app()
            self.app.dependency_overrides[get_capability_service] = (
                lambda: self.capability_service
            )
            self.app.dependency_overrides[get_forecast_service] = (
                lambda: self.forecast_service
            )
            self.app.dependency_overrides[get_audit_service] = lambda: self.audit_service
            self.app.dependency_overrides[get_checkpoint_service] = (
                lambda: self.checkpoint_service
            )
            self.app.dependency_overrides[get_rollback_service] = (
                lambda: self.rollback_service
            )
            self.client = TestClient(self.app, raise_server_exceptions=False)

    return Context()


@pytest.fixture
def client(context) -> TestClient:
    return context.client


def request_body(**overrides) -> Dict:
    body = {
        "store_id": "store_001",
        "model_version": "latest",
        "horizon_days": HORIZON_DAYS,
        "history": [
            {
                "date": (HISTORY_START + timedelta(days=offset)).isoformat(),
                "sales": 500.0 + offset,
                "store_id": "store_001",
            }
            for offset in range(HISTORY_DAYS)
        ],
        "scenario": {
            "scenario_type": "promotion",
            "start_date": FORECAST_START.isoformat(),
            "end_date": (FORECAST_START + timedelta(days=2)).isoformat(),
            "promotion_intensity": "high",
            "discount_depth": 0.25,
        },
    }
    body.update(overrides)
    return body


def seed_completed_scenario(
    audit_service: ScenarioAuditService,
    checkpoint_service: ForecastCheckpointService,
    scenario_id: str = SCENARIO_ID,
    expired: bool = False,
) -> str:
    """Store a completed comparison with a real baseline checkpoint."""
    checkpoint = checkpoint_service.create(
        forecast_id=BASELINE_ID,
        store_id="store_001",
        model_version="14",
        model_stage="Production",
        horizon_days=HORIZON_DAYS,
        history_start_date=HISTORY_START,
        history_end_date=HISTORY_START + timedelta(days=HISTORY_DAYS - 1),
        forecast_start_date=FORECAST_START,
        forecast_end_date=FORECAST_START + timedelta(days=HORIZON_DAYS - 1),
        data_source="kaggle_m5",
        dataset_version="v1.0.0",
        feature_schema_version="v1",
        expires_at=datetime.now(timezone.utc)
        + (timedelta(hours=-1) if expired else timedelta(hours=6)),
    )
    checkpoint_service.mark_completed(checkpoint.forecast_id)

    comparison = build_comparison(scenario_id=scenario_id)
    audit_service.record(
        request_id="req_seed",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.SCENARIO_COMPLETED,
        request_payload=request_body(),
        scenario_id=scenario_id,
        baseline_forecast_id=checkpoint.forecast_id,
        response_summary=comparison.model_dump(mode="json"),
    )
    return scenario_id


# --------------------------------------------------------------------------
# App wiring
# --------------------------------------------------------------------------


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_every_scenario_route_is_tagged():
    app = create_app()
    paths = {
        route.path: getattr(route, "tags", [])
        for route in app.routes
        if getattr(route, "path", "").startswith("/v1/scenario")
    }

    assert len(paths) == 6
    for path, tags in paths.items():
        assert tags == ["Scenario Planning"], path


def test_openapi_documents_response_models():
    schema = create_app().openapi()

    assert {"name": "Scenario Planning"} in [
        {"name": tag["name"]} for tag in schema["tags"]
    ]
    compare = schema["paths"]["/v1/scenario/compare"]["post"]
    ref = compare["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("ScenarioComparisonResponse")

    for path, method in [
        ("/v1/scenario/capabilities", "get"),
        ("/v1/scenario/baseline/{baseline_forecast_id}", "get"),
        ("/v1/scenario/{scenario_id}", "get"),
        ("/v1/scenario/{scenario_id}/rollback", "post"),
        ("/v1/scenario/{scenario_id}/audit", "get"),
    ]:
        responses = schema["paths"][path][method]["responses"]
        assert "$ref" in responses["200"]["content"]["application/json"]["schema"], path


# --------------------------------------------------------------------------
# GET /capabilities
# --------------------------------------------------------------------------


def test_capabilities_returns_supported_types(client, context):
    response = client.get("/v1/scenario/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["model_version"] == "14"
    assert len(body["capabilities"]) == len(ScenarioType)
    promotion = next(
        item for item in body["capabilities"] if item["scenario_type"] == "promotion"
    )
    assert promotion["supported"] is True
    assert promotion["required_features"] == ["is_promotion"]
    assert promotion["available_features"] == ["is_promotion"]
    assert promotion["missing_features"] == []
    assert context.capability_service.calls == ["latest"]


def test_capabilities_honours_model_version_query(client, context):
    client.get("/v1/scenario/capabilities", params={"model_version": "21"})
    assert context.capability_service.calls == ["21"]


def test_capabilities_reports_missing_features(client, context):
    context.capability_service.supported = False

    body = client.get("/v1/scenario/capabilities").json()

    promotion = next(
        item for item in body["capabilities"] if item["scenario_type"] == "promotion"
    )
    assert promotion["supported"] is False
    assert promotion["missing_features"] == ["is_promotion"]
    assert "was not trained" in promotion["reason"]


def test_capabilities_returns_404_for_unknown_model(client, context):
    context.capability_service.error = ModelNotFoundError(field="model_version")

    response = client.get("/v1/scenario/capabilities", params={"model_version": "99"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"


def test_capabilities_returns_503_when_mlflow_is_down(client, context):
    context.capability_service.error = MLflowUnavailableError()

    response = client.get("/v1/scenario/capabilities")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MLFLOW_UNAVAILABLE"


def test_capabilities_returns_409_without_a_feature_schema(client, context):
    context.capability_service.error = FeatureSchemaError(
        FeatureSchemaErrorCode.FEATURE_SCHEMA_MISSING,
        "Feature schema is not available for this model version.",
        field="model_version",
    )

    response = client.get("/v1/scenario/capabilities")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FEATURE_SCHEMA_MISSING"


# --------------------------------------------------------------------------
# POST /compare
# --------------------------------------------------------------------------


def test_successful_promotion_scenario(client, context):
    response = client.post("/v1/scenario/compare", json=request_body())

    assert response.status_code == 200
    body = response.json()
    assert body["scenario_id"] == SCENARIO_ID
    assert body["forecast_type"] == "predictive_what_if"
    assert body["impact"]["total_difference"] == pytest.approx(150.0)
    assert body["impact"]["positive_days"] == HORIZON_DAYS
    assert len(body["daily_forecasts"]) == HORIZON_DAYS
    assert len(body["limitations"]) == 4
    assert response.headers[REQUEST_ID_HEADER].startswith("req_")


def test_successful_comparison_is_audited(client, context):
    client.post("/v1/scenario/compare", json=request_body())

    events = context.audit_service.get_by_scenario_id(SCENARIO_ID)
    assert [event.status for event in events] == ["scenario_completed"]
    assert events[0].duration_ms is not None
    assert events[0].response_summary["scenario_id"] == SCENARIO_ID


def test_received_event_is_recorded_before_work_starts(client, context):
    client.post("/v1/scenario/compare", json=request_body())

    statuses = [
        event.status for event in context.audit_service.list_for_store("store_001")
    ]
    assert "received" in statuses
    assert "scenario_completed" in statuses


def test_capability_check_runs_before_the_forecast(client, context):
    context.capability_service.supported = False

    response = client.post("/v1/scenario/compare", json=request_body())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCENARIO_NOT_SUPPORTED"
    # The expensive step never ran.
    assert context.forecast_service.calls == []


def test_unsupported_promotion_scenario_is_audited(client, context):
    context.capability_service.supported = False

    client.post("/v1/scenario/compare", json=request_body())

    statuses = [
        event.status for event in context.audit_service.list_for_store("store_001")
    ]
    assert "capability_check_failed" in statuses


def test_unsupported_scenario_explains_the_gap(client, context):
    context.capability_service.supported = False

    details = client.post("/v1/scenario/compare", json=request_body()).json()["error"]
    assert details["field"] == "scenario.scenario_type"
    assert details["details"]["missing_features"] == ["is_promotion"]


@pytest.mark.parametrize(
    "mutation,field_fragment",
    [
        ({"history": []}, "history"),
        ({"horizon_days": 0}, "horizon_days"),
        ({"horizon_days": 500}, "horizon_days"),
        ({"confidence_levels": [0.9, 0.1]}, "confidence_levels"),
        ({"store_id": ""}, "store_id"),
    ],
)
def test_invalid_request_returns_422(client, mutation, field_fragment):
    response = client.post("/v1/scenario/compare", json=request_body(**mutation))

    assert response.status_code == 422
    assert field_fragment in response.text


def test_invalid_history_too_few_rows_returns_422(client):
    short = request_body()
    short["history"] = short["history"][:3]

    response = client.post("/v1/scenario/compare", json=short)

    assert response.status_code == 422
    assert "history" in response.text


def test_invalid_history_unsorted_returns_422(client):
    body = request_body()
    body["history"][0], body["history"][5] = body["history"][5], body["history"][0]

    response = client.post("/v1/scenario/compare", json=body)

    assert response.status_code == 422


def test_missing_model_returns_404(client, context):
    context.forecast_service.error = ModelNotFoundError(
        "Requested model version was not found.", field="model_version"
    )

    response = client.post("/v1/scenario/compare", json=request_body())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"


def test_missing_model_marks_the_attempt_failed(client, context):
    context.forecast_service.error = ModelNotFoundError(field="model_version")

    client.post("/v1/scenario/compare", json=request_body())

    statuses = [
        event.status for event in context.audit_service.list_for_store("store_001")
    ]
    assert "baseline_failed" in statuses


def test_prediction_failure_returns_500_and_audits(client, context):
    context.forecast_service.error = PredictionFailedError()

    response = client.post("/v1/scenario/compare", json=request_body())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "PREDICTION_FAILED"
    statuses = [
        event.status for event in context.audit_service.list_for_store("store_001")
    ]
    assert "scenario_failed" in statuses


def test_unexpected_error_returns_500_without_leaking(client, context):
    context.forecast_service.error = RuntimeError("boom at /usr/local/airflow/x.py")

    response = client.post("/v1/scenario/compare", json=request_body())

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_SCENARIO_ERROR"
    assert body["error"]["details"] == {}
    assert "/usr/local/airflow" not in response.text
    assert "Traceback" not in response.text


def test_override_engine_rejection_returns_409(client, context):
    context.forecast_service.error = OverrideNotSupportedError(
        "The scenario window does not overlap the forecast horizon."
    )

    response = client.post("/v1/scenario/compare", json=request_body())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCENARIO_NOT_SUPPORTED"


def test_baseline_reuse_is_passed_through(client, context):
    response = client.post(
        "/v1/scenario/compare", json=request_body(baseline_forecast_id="bf_existing")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["baseline_forecast_id"] == "bf_existing"
    assert body["baseline_reused"] is True
    assert context.forecast_service.calls[0].baseline_forecast_id == "bf_existing"


def test_inference_adapters_are_wired():
    """
    The scenario adapters are implemented, not placeholders.

    This previously asserted the opposite -- that the two ports had no
    implementation and the route failed with "inference is not configured".
    Both are now backed by `ScenarioMatrixBuilder` and
    `RecursiveMatrixPredictor`, so the assertion is inverted: the dependency
    resolves to a real object, and the route gets as far as needing a model.
    """
    from api.dependencies.scenario import (
        get_future_feature_builder,
        get_matrix_predictor,
    )
    from api.services.scenario_matrix_service import (
        RecursiveMatrixPredictor,
        ScenarioMatrixBuilder,
    )

    builder = get_future_feature_builder()
    predictor = get_matrix_predictor()

    assert isinstance(builder, ScenarioMatrixBuilder)
    assert isinstance(predictor, RecursiveMatrixPredictor)
    # The predictor must share the builder's instance: it reuses that run's
    # baseline projection instead of resolving and loading the model again.
    assert predictor.builder_source is builder


def test_compare_without_a_production_model_reports_that():
    """
    With the adapters wired, the first real obstacle is model resolution.

    The failure is a 404 naming the missing Production version -- not a 500
    about unconfigured inference, which is what it used to be.
    """
    app = create_app()
    app.dependency_overrides[get_capability_service] = lambda: FakeCapabilityService()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/v1/scenario/compare", json=request_body())

    assert response.status_code in (404, 500, 503)
    body = response.json()
    assert body["success"] is False
    # Whatever the upstream state, the message must not claim the feature is
    # unimplemented.
    assert "not configured" not in body["error"]["message"].lower()


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_idempotent_retry_returns_the_same_scenario(client, context):
    headers = {"Idempotency-Key": "key-1"}

    first = client.post("/v1/scenario/compare", json=request_body(), headers=headers)
    assert first.status_code == 200

    # A second, different comparison would be returned if the key were ignored.
    context.forecast_service.comparison = build_comparison(scenario_id="scn_different")
    second = client.post("/v1/scenario/compare", json=request_body(), headers=headers)

    assert second.status_code == 200
    assert second.json()["scenario_id"] == first.json()["scenario_id"] == SCENARIO_ID
    # The forecast ran only once.
    assert len(context.forecast_service.calls) == 1


def test_idempotency_conflict_returns_409(client, context):
    headers = {"Idempotency-Key": "key-1"}
    client.post("/v1/scenario/compare", json=request_body(), headers=headers)

    response = client.post(
        "/v1/scenario/compare", json=request_body(horizon_days=30), headers=headers
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert body["error"]["field"] == "Idempotency-Key"


def test_different_keys_run_independently(client, context):
    client.post(
        "/v1/scenario/compare",
        json=request_body(),
        headers={"Idempotency-Key": "key-a"},
    )
    client.post(
        "/v1/scenario/compare",
        json=request_body(),
        headers={"Idempotency-Key": "key-b"},
    )

    assert len(context.forecast_service.calls) == 2


def test_no_key_means_no_replay(client, context):
    client.post("/v1/scenario/compare", json=request_body())
    client.post("/v1/scenario/compare", json=request_body())

    assert len(context.forecast_service.calls) == 2


# --------------------------------------------------------------------------
# GET /baseline/{id}
# --------------------------------------------------------------------------


def test_get_baseline_returns_metadata(client, context):
    seed_completed_scenario(context.audit_service, context.checkpoint_service)

    response = client.get(f"/v1/scenario/baseline/{BASELINE_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["forecast_id"] == BASELINE_ID
    assert body["status"] == "completed"
    assert body["data_source"] == "kaggle_m5"
    assert body["model_stage"] == "Production"


def test_get_unknown_baseline_returns_404(client):
    response = client.get("/v1/scenario/baseline/bf_nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BASELINE_NOT_FOUND"


# --------------------------------------------------------------------------
# GET /{scenario_id}
# --------------------------------------------------------------------------


def test_get_stored_scenario(client, context):
    seed_completed_scenario(context.audit_service, context.checkpoint_service)

    response = client.get(f"/v1/scenario/{SCENARIO_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["scenario_id"] == SCENARIO_ID
    assert body["forecast_type"] == "predictive_what_if"
    assert len(body["daily_forecasts"]) == HORIZON_DAYS


def test_stored_scenario_is_retrievable_after_compare(client, context):
    created = client.post("/v1/scenario/compare", json=request_body()).json()

    fetched = client.get(f"/v1/scenario/{created['scenario_id']}")

    assert fetched.status_code == 200
    assert fetched.json()["scenario_id"] == created["scenario_id"]


def test_get_unknown_scenario_returns_404(client):
    response = client.get("/v1/scenario/scn_nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCENARIO_NOT_FOUND"


def test_get_expired_scenario_returns_410(client, context):
    seed_completed_scenario(
        context.audit_service, context.checkpoint_service, expired=True
    )

    response = client.get(f"/v1/scenario/{SCENARIO_ID}")

    assert response.status_code == 410
    body = response.json()
    assert body["error"]["code"] == "SCENARIO_EXPIRED"
    assert body["error"]["details"]["scenario_id"] == SCENARIO_ID


def test_failed_scenario_has_no_stored_comparison(client, context):
    context.audit_service.record(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.SCENARIO_FAILED,
        request_payload=request_body(),
        scenario_id="scn_failed",
        error_code="PREDICTION_FAILED",
    )

    response = client.get("/v1/scenario/scn_failed")

    assert response.status_code == 404
    assert response.json()["error"]["details"]["recorded_statuses"] == ["scenario_failed"]


# --------------------------------------------------------------------------
# POST /{scenario_id}/rollback
# --------------------------------------------------------------------------


def test_rollback_returns_confirmation(client, context):
    seed_completed_scenario(context.audit_service, context.checkpoint_service)

    response = client.post(
        f"/v1/scenario/{SCENARIO_ID}/rollback", json={"reason": "assumptions were wrong"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["rolled_back"] is True
    assert body["status"] == "rolled_back"
    assert body["reason"] == "assumptions were wrong"
    assert body["audit_history_preserved"] is True
    assert body["baseline_preserved"] is True


def test_rollback_preserves_audit_history(client, context):
    seed_completed_scenario(context.audit_service, context.checkpoint_service)

    client.post(f"/v1/scenario/{SCENARIO_ID}/rollback", json={"reason": "withdrawn"})

    events = context.audit_service.get_by_scenario_id(SCENARIO_ID)
    assert [event.status for event in events] == ["scenario_completed", "rolled_back"]


def test_rollback_does_not_delete_the_baseline(client, context):
    seed_completed_scenario(context.audit_service, context.checkpoint_service)

    client.post(f"/v1/scenario/{SCENARIO_ID}/rollback", json={"reason": "withdrawn"})

    assert client.get(f"/v1/scenario/baseline/{BASELINE_ID}").status_code == 200


def test_rollback_requires_a_reason(client):
    assert client.post(f"/v1/scenario/{SCENARIO_ID}/rollback", json={}).status_code == 422
    assert (
        client.post(
            f"/v1/scenario/{SCENARIO_ID}/rollback", json={"reason": "x"}
        ).status_code
        == 422
    )


def test_rollback_rejects_unknown_body_fields(client):
    response = client.post(
        f"/v1/scenario/{SCENARIO_ID}/rollback",
        json={"reason": "valid reason", "force": True},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# GET /{scenario_id}/audit
# --------------------------------------------------------------------------


def test_audit_retrieval_returns_events(client, context):
    seed_completed_scenario(context.audit_service, context.checkpoint_service)
    client.post(f"/v1/scenario/{SCENARIO_ID}/rollback", json={"reason": "withdrawn"})

    response = client.get(f"/v1/scenario/{SCENARIO_ID}/audit")

    assert response.status_code == 200
    body = response.json()
    assert body["scenario_id"] == SCENARIO_ID
    assert body["event_count"] == 2
    assert [event["status"] for event in body["events"]] == [
        "scenario_completed",
        "rolled_back",
    ]
    first = body["events"][0]
    assert first["store_id"] == "store_001"
    assert first["model_version"] == "14"
    assert first["created_at"]


def test_audit_response_omits_payloads(client, context):
    seed_completed_scenario(context.audit_service, context.checkpoint_service)

    body = client.get(f"/v1/scenario/{SCENARIO_ID}/audit").json()

    # Neither the caller's history nor the full comparison is echoed back.
    assert "request_payload" not in body["events"][0]
    assert "response_summary" not in body["events"][0]


def test_audit_for_unknown_scenario_returns_404(client):
    response = client.get("/v1/scenario/scn_nope/audit")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCENARIO_NOT_FOUND"


def test_audit_records_failures_too(client, context):
    context.forecast_service.error = PredictionFailedError()
    client.post("/v1/scenario/compare", json=request_body())

    events = context.audit_service.list_for_store("store_001")
    failed = [event for event in events if event.status == "scenario_failed"]
    assert failed
    assert failed[0].error_code == "PREDICTION_FAILED"


# --------------------------------------------------------------------------
# Cross-cutting
# --------------------------------------------------------------------------


def test_request_id_is_echoed_on_every_route(client, context):
    seed_completed_scenario(context.audit_service, context.checkpoint_service)

    for method, path in [
        ("get", "/v1/scenario/capabilities"),
        ("get", f"/v1/scenario/baseline/{BASELINE_ID}"),
        ("get", f"/v1/scenario/{SCENARIO_ID}"),
        ("get", f"/v1/scenario/{SCENARIO_ID}/audit"),
    ]:
        response = getattr(client, method)(
            path, headers={REQUEST_ID_HEADER: "req_client"}
        )
        assert response.headers[REQUEST_ID_HEADER] == "req_client", path


def test_error_envelope_is_consistent_across_routes(client, context):
    context.capability_service.error = ModelNotFoundError()

    body = client.get("/v1/scenario/capabilities").json()

    assert body["success"] is False
    assert set(body["error"]) == {
        "code",
        "message",
        "field",
        "details",
        "request_id",
        "timestamp",
    }
