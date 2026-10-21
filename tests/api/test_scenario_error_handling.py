"""Tests for the error taxonomy, the HTTP error boundary, and request ids."""

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.core.errors import (  # noqa: E402
    ERROR_STATUS_MAP,
    GENERIC_INTERNAL_MESSAGE,
    REDACTED,
    ArtifactDownloadFailedError,
    DatabaseUnavailableError,
    IdempotencyKeyConflictError,
    InternalScenarioError,
    MLflowUnavailableError,
    ModelNotFoundError,
    ModelNotInProductionError,
    PredictionFailedError,
    ScenarioErrorCode,
    ScenarioNotSupportedError,
    ScenarioPlannerError,
    ScenarioValidationError,
    map_legacy_code,
    new_request_id,
    normalize_error,
    sanitize_details,
    sanitize_text,
)
from api.middleware.error_handler import (  # noqa: E402
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    build_error_payload,
    install_error_handlers,
    resolve_status,
    sanitize_request_id,
)
from api.services.baseline_forecast_service import (  # noqa: E402
    BaselineForecastError,
    BaselineForecastErrorCode,
)
from api.services.feature_schema_service import (  # noqa: E402
    FeatureSchemaError,
    FeatureSchemaErrorCode,
)
from api.services.forecast_checkpoint_service import (  # noqa: E402
    CheckpointError,
    CheckpointErrorCode,
)
from api.services.scenario_feature_service import (  # noqa: E402
    ScenarioNotSupportedError as OverrideNotSupportedError,
)
from api.services.scenario_impact_service import (  # noqa: E402
    ScenarioImpactError,
    ScenarioImpactErrorCode,
)
from api.services.scenario_validation import (  # noqa: E402
    ScenarioErrorCode as ValidationErrorCode,
)
from api.services.scenario_validation import (  # noqa: E402
    ScenarioValidationError as ServiceValidationError,
)

REQUIRED_CODES = {
    "SCENARIO_VALIDATION_ERROR",
    "SCENARIO_NOT_SUPPORTED",
    "FEATURE_SCHEMA_MISSING",
    "FEATURE_NOT_AVAILABLE",
    "MODEL_NOT_FOUND",
    "MODEL_NOT_IN_PRODUCTION",
    "MLFLOW_UNAVAILABLE",
    "ARTIFACT_DOWNLOAD_FAILED",
    "PREDICTION_FAILED",
    "FORECAST_ALIGNMENT_ERROR",
    "DATABASE_UNAVAILABLE",
    "BASELINE_NOT_FOUND",
    "BASELINE_EXPIRED",
    "SCENARIO_ALREADY_RUNNING",
    "INTERNAL_SCENARIO_ERROR",
}

SECRETS = [
    "postgresql://mlflow:mlflow@mlflow-db:5432/mlflow",
    "/usr/local/airflow/include/config/ml_config.yaml",
    "http://minio:9000",
    "minioadmin",
    "AKIAIOSFODNN7EXAMPLE",
]


# --------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------


def test_taxonomy_contains_every_required_code():
    assert REQUIRED_CODES <= {code.value for code in ScenarioErrorCode}


def test_every_code_has_a_status():
    for code in ScenarioErrorCode:
        assert code in ERROR_STATUS_MAP, code


@pytest.mark.parametrize(
    "error,expected_status",
    [
        (ScenarioValidationError(), 422),
        (ScenarioNotSupportedError(), 409),
        (ModelNotFoundError(), 404),
        (ModelNotInProductionError(), 409),
        (MLflowUnavailableError(), 503),
        (ArtifactDownloadFailedError(), 502),
        (PredictionFailedError(), 500),
        (DatabaseUnavailableError(), 503),
        (InternalScenarioError(), 500),
        (IdempotencyKeyConflictError(), 409),
    ],
)
def test_status_mapping_matches_specification(error, expected_status):
    assert error.status_code == expected_status
    assert resolve_status(error) == expected_status


def test_model_not_found_default_message():
    assert ModelNotFoundError().message == "Requested model version was not found."


# --------------------------------------------------------------------------
# Response envelope
# --------------------------------------------------------------------------


def test_error_payload_has_the_documented_structure():
    error = ModelNotFoundError(field="model_version", details={"model_version": "99"})

    payload = error.to_error_payload(request_id="req_abc")

    assert payload["success"] is False
    assert set(payload.keys()) == {"success", "error"}
    assert set(payload["error"].keys()) == {
        "code",
        "message",
        "field",
        "details",
        "request_id",
        "timestamp",
    }
    assert payload["error"]["code"] == "MODEL_NOT_FOUND"
    assert payload["error"]["message"] == "Requested model version was not found."
    assert payload["error"]["field"] == "model_version"
    assert payload["error"]["details"] == {"model_version": "99"}
    assert payload["error"]["request_id"] == "req_abc"


def test_timestamp_is_utc_iso_with_z_suffix():
    payload = ModelNotFoundError().to_error_payload(request_id="req_abc")
    timestamp = payload["error"]["timestamp"]

    assert timestamp.endswith("Z")
    assert len(timestamp) == 20
    from datetime import datetime

    datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")


def test_request_ids_are_prefixed_and_unique():
    first, second = new_request_id(), new_request_id()
    assert first.startswith("req_")
    assert first != second


# --------------------------------------------------------------------------
# Normalising service errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_code,expected",
    [
        ("FEATURE_SCHEMA_MISSING", "FEATURE_SCHEMA_MISSING"),
        ("FEATURE_SCHEMA_INVALID", "ARTIFACT_DOWNLOAD_FAILED"),
        ("MODEL_VERSION_NOT_FOUND", "MODEL_NOT_FOUND"),
        ("MODEL_NOT_IN_PRODUCTION", "MODEL_NOT_IN_PRODUCTION"),
        ("MLFLOW_UNAVAILABLE", "MLFLOW_UNAVAILABLE"),
        # The taxonomy gained a dedicated code for this so the forecast UI
        # can offer "add more history" guidance; the status stays 422.
        ("INSUFFICIENT_HISTORY", "INSUFFICIENT_HISTORY"),
        ("PREDICTION_FAILED", "PREDICTION_FAILED"),
        ("DATABASE_NOT_CONFIGURED", "DATABASE_UNAVAILABLE"),
        ("CHECKPOINT_NOT_FOUND", "BASELINE_NOT_FOUND"),
        ("BASELINE_NOT_REUSABLE", "BASELINE_EXPIRED"),
        ("FORECAST_ALIGNMENT_ERROR", "FORECAST_ALIGNMENT_ERROR"),
        ("MISSING_FORECAST_VALUES", "PREDICTION_FAILED"),
        ("TARGET_COLUMN_MODIFICATION_ATTEMPTED", "SCENARIO_VALIDATION_ERROR"),
        ("history_too_short", "SCENARIO_VALIDATION_ERROR"),
        ("unsupported_scenario_type", "SCENARIO_NOT_SUPPORTED"),
        ("WHO_KNOWS", "INTERNAL_SCENARIO_ERROR"),
    ],
)
def test_legacy_codes_map_into_the_taxonomy(raw_code, expected):
    assert map_legacy_code(raw_code).value == expected


def test_service_validation_error_normalises_to_422():
    exc = ServiceValidationError(
        ValidationErrorCode.HISTORY_TOO_SHORT,
        "history must contain at least 7 rows, received 3",
        field="history",
        details={"received": 3},
    )

    error = normalize_error(exc)

    assert error.status_code == 422
    assert error.error_code == "SCENARIO_VALIDATION_ERROR"
    assert error.field == "history"
    assert error.details["received"] == 3
    # The original code is kept so logs and clients can still correlate.
    assert error.details["source_error_code"] == "history_too_short"


def test_feature_schema_error_normalises_to_409():
    exc = FeatureSchemaError(
        FeatureSchemaErrorCode.FEATURE_SCHEMA_MISSING,
        "Feature schema is not available for this model version.",
        field="model_version",
    )

    error = normalize_error(exc)

    assert error.status_code == 409
    assert error.error_code == "FEATURE_SCHEMA_MISSING"
    assert error.message == "Feature schema is not available for this model version."


@pytest.mark.parametrize(
    "exc,expected_code,expected_status",
    [
        (
            BaselineForecastError(
                BaselineForecastErrorCode.MODEL_NOT_IN_PRODUCTION, "nope"
            ),
            "MODEL_NOT_IN_PRODUCTION",
            409,
        ),
        (
            BaselineForecastError(BaselineForecastErrorCode.MLFLOW_UNAVAILABLE, "down"),
            "MLFLOW_UNAVAILABLE",
            503,
        ),
        (
            CheckpointError(CheckpointErrorCode.DATABASE_UNAVAILABLE, "no db"),
            "DATABASE_UNAVAILABLE",
            503,
        ),
        (
            ScenarioImpactError(
                ScenarioImpactErrorCode.FORECAST_ALIGNMENT_ERROR, "misaligned"
            ),
            "FORECAST_ALIGNMENT_ERROR",
            500,
        ),
        (OverrideNotSupportedError("cannot"), "SCENARIO_NOT_SUPPORTED", 409),
    ],
)
def test_service_errors_normalise(exc, expected_code, expected_status):
    error = normalize_error(exc)
    assert error.error_code == expected_code
    assert error.status_code == expected_status


def test_canonical_errors_pass_through_normalisation():
    error = ModelNotFoundError(field="model_version")
    assert normalize_error(error) is error


def test_plain_exception_becomes_an_internal_error():
    error = normalize_error(ValueError("something odd"))

    assert error.error_code == "INTERNAL_SCENARIO_ERROR"
    assert error.status_code == 500
    assert error.details == {"exception_type": "ValueError"}


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("secret", SECRETS)
def test_secrets_are_redacted_from_text(secret):
    cleaned = sanitize_text(f"failure contacting {secret} during startup")
    assert secret not in cleaned
    assert REDACTED in cleaned


def test_sensitive_and_location_keys_are_redacted():
    details = sanitize_details(
        {
            "password": "hunter2",
            "aws_secret_access_key": "abc123",
            "tracking_uri": "http://mlflow:5001",
            "raw_path": "/usr/local/airflow/data",
            "artifact_path": "feature_schema.json",
            "traceback": "Traceback (most recent call last) ...",
            "host": "mlflow-db",
            "run_id": "abc",
            "rows": 7,
        }
    )

    for key in (
        "password",
        "aws_secret_access_key",
        "tracking_uri",
        "raw_path",
        "artifact_path",
        "traceback",
        "host",
    ):
        assert details[key] == REDACTED, key
    # Harmless fields survive.
    assert details["run_id"] == "abc"
    assert details["rows"] == 7


def test_redaction_recurses_into_nested_structures():
    details = sanitize_details(
        {"outer": {"inner": ["/tmp/scalers.pkl", {"dsn": "postgresql://u:p@h/db"}]}}
    )

    rendered = str(details)
    assert "/tmp/scalers.pkl" not in rendered
    assert "postgresql://" not in rendered


def test_database_error_payload_hides_the_connection_string():
    exc = CheckpointError(
        CheckpointErrorCode.DATABASE_UNAVAILABLE,
        "could not connect to postgresql://mlflow:mlflow@mlflow-db:5432/mlflow",
        details={"error": "FATAL: password authentication failed for user 'mlflow'"},
    )

    payload = build_error_payload(exc, request_id="req_x")
    rendered = str(payload)

    assert "mlflow-db" not in rendered
    assert "postgresql://" not in rendered
    assert payload["error"]["code"] == "DATABASE_UNAVAILABLE"


def test_internal_errors_do_not_leak_stack_traces_or_detail():
    class Boom(Exception):
        pass

    try:
        raise Boom("object has no attribute '_scaler' at /usr/local/airflow/x.py line 42")
    except Boom as exc:
        payload = build_error_payload(exc, request_id="req_x")

    error = payload["error"]
    assert error["code"] == "INTERNAL_SCENARIO_ERROR"
    assert error["message"] == GENERIC_INTERNAL_MESSAGE
    assert error["details"] == {}
    rendered = str(payload)
    assert "Traceback" not in rendered
    assert "_scaler" not in rendered
    assert "/usr/local/airflow" not in rendered
    assert "Boom" not in rendered


# --------------------------------------------------------------------------
# Request id handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inbound,expected",
    [
        ("req_client_123", "req_client_123"),
        ("  req_spaced  ", "req_spaced"),
        ("bad\r\ninjected: header", "badinjected:header"),
        ("", None),
        (None, None),
    ],
)
def test_inbound_request_ids_are_sanitised(inbound, expected):
    result = sanitize_request_id(inbound)
    if expected is None:
        assert result.startswith("req_")
    else:
        assert result == expected
    assert "\n" not in result and "\r" not in result


def test_overlong_request_id_is_truncated():
    assert len(sanitize_request_id("a" * 500)) == 128


# --------------------------------------------------------------------------
# HTTP boundary (real ASGI app)
# --------------------------------------------------------------------------


def build_app() -> FastAPI:
    """A minimal app whose routes raise, to exercise the error boundary."""
    app = FastAPI()

    @app.get("/ok")
    def ok():
        from api.middleware.error_handler import get_request_id

        return {"success": True, "request_id": get_request_id()}

    @app.get("/validation")
    def validation():
        raise ServiceValidationError(
            ValidationErrorCode.HISTORY_TOO_SHORT,
            "history must contain at least 7 rows, received 3",
            field="history",
            details={"received": 3},
        )

    @app.get("/not-supported")
    def not_supported():
        raise OverrideNotSupportedError("This model cannot express a promotion scenario.")

    @app.get("/model-missing")
    def model_missing():
        raise ModelNotFoundError(field="model_version", details={"model_version": "99"})

    @app.get("/not-production")
    def not_production():
        raise ModelNotInProductionError(details={"model_stage": "Staging"})

    @app.get("/mlflow-down")
    def mlflow_down():
        raise MLflowUnavailableError(details={"tracking_uri": "http://mlflow:5001"})

    @app.get("/artifact-failed")
    def artifact_failed():
        raise ArtifactDownloadFailedError(details={"path": "/tmp/models/x.pkl"})

    @app.get("/prediction-failed")
    def prediction_failed():
        raise PredictionFailedError()

    @app.get("/db-down")
    def db_down():
        raise CheckpointError(
            CheckpointErrorCode.DATABASE_UNAVAILABLE,
            "could not connect to postgresql://mlflow:mlflow@mlflow-db:5432/mlflow",
        )

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internals at /usr/local/airflow/include/x.py")

    @app.get("/conflict")
    def conflict():
        raise IdempotencyKeyConflictError(field="Idempotency-Key")

    return install_error_handlers(app)


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(), raise_server_exceptions=False)


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("/validation", 422, "SCENARIO_VALIDATION_ERROR"),
        ("/not-supported", 409, "SCENARIO_NOT_SUPPORTED"),
        ("/model-missing", 404, "MODEL_NOT_FOUND"),
        ("/not-production", 409, "MODEL_NOT_IN_PRODUCTION"),
        ("/mlflow-down", 503, "MLFLOW_UNAVAILABLE"),
        ("/artifact-failed", 502, "ARTIFACT_DOWNLOAD_FAILED"),
        ("/prediction-failed", 500, "PREDICTION_FAILED"),
        ("/db-down", 503, "DATABASE_UNAVAILABLE"),
        ("/boom", 500, "INTERNAL_SCENARIO_ERROR"),
        ("/conflict", 409, "IDEMPOTENCY_KEY_CONFLICT"),
    ],
)
def test_http_status_and_code_for_each_handler(client, path, status, code):
    response = client.get(path)

    assert response.status_code == status
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == code
    assert body["error"]["request_id"].startswith("req_")
    assert body["error"]["timestamp"].endswith("Z")


def test_response_always_carries_the_request_id_header(client):
    response = client.get("/ok")
    assert response.headers[REQUEST_ID_HEADER].startswith("req_")

    error_response = client.get("/model-missing")
    header_id = error_response.headers[REQUEST_ID_HEADER]
    assert error_response.json()["error"]["request_id"] == header_id


def test_inbound_request_id_is_honoured_end_to_end(client):
    response = client.get("/model-missing", headers={REQUEST_ID_HEADER: "req_from_client"})

    assert response.headers[REQUEST_ID_HEADER] == "req_from_client"
    assert response.json()["error"]["request_id"] == "req_from_client"


def test_request_id_is_visible_to_the_endpoint(client):
    response = client.get("/ok", headers={REQUEST_ID_HEADER: "req_ctx"})
    assert response.json()["request_id"] == "req_ctx"


def test_header_injection_attempt_is_neutralised(client):
    response = client.get("/ok", headers={REQUEST_ID_HEADER: "abc-123.def"})
    assert response.headers[REQUEST_ID_HEADER] == "abc-123.def"


def test_unexpected_error_response_leaks_nothing(client):
    response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["message"] == GENERIC_INTERNAL_MESSAGE
    assert body["error"]["details"] == {}
    rendered = response.text
    for marker in ("Traceback", "/usr/local/airflow", "RuntimeError", "secret internals"):
        assert marker not in rendered


@pytest.mark.parametrize("path", ["/mlflow-down", "/artifact-failed", "/db-down"])
def test_infrastructure_details_are_redacted_over_http(client, path):
    rendered = client.get(path).text

    for marker in ("mlflow:5001", "/tmp/models", "postgresql://", "mlflow-db"):
        assert marker not in rendered


def test_successful_requests_are_untouched(client):
    response = client.get("/ok")
    assert response.status_code == 200
    assert response.json()["success"] is True
