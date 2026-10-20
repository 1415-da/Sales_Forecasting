"""Tests for audit logging, rollback behaviour, and idempotency."""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.core.errors import (  # noqa: E402
    DatabaseUnavailableError,
    IdempotencyKeyConflictError,
    PredictionFailedError,
    ScenarioAlreadyRunningError,
)
from api.schemas.baseline_forecast import BaselineForecastStatus  # noqa: E402
from api.services.baseline_forecast_service import (  # noqa: E402
    BaselineForecastError,
    BaselineForecastErrorCode,
)
from api.services.forecast_checkpoint_service import ForecastCheckpointService  # noqa: E402
from api.services.scenario_audit_service import (  # noqa: E402
    TABLE_NAME,
    ScenarioAuditEvent,
    ScenarioAuditService,
    ScenarioAuditStatus,
    compute_request_hash,
)
from api.services.scenario_rollback_service import (  # noqa: E402
    RollbackResponse,
    ScenarioLockRegistry,
    ScenarioRollbackService,
    ScenarioStage,
)
from api.services.scenario_validation import (  # noqa: E402
    ScenarioErrorCode as ValidationErrorCode,
)
from api.services.scenario_validation import (  # noqa: E402
    ScenarioValidationError as ServiceValidationError,
)

HISTORY_START = date(2026, 1, 1)
REQUEST_PAYLOAD = {
    "store_id": "store_001",
    "horizon_days": 7,
    "scenario": {"scenario_type": "promotion", "promotion_intensity": "high"},
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def database_url(tmp_path) -> str:
    """A file-backed SQLite database shared by both services."""
    return f"sqlite+pysqlite:///{tmp_path / 'scenario.db'}"


@pytest.fixture
def audit_service(database_url) -> ScenarioAuditService:
    return ScenarioAuditService(database_url=database_url, create_tables=True)


@pytest.fixture
def checkpoint_service(database_url) -> ForecastCheckpointService:
    return ForecastCheckpointService(database_url=database_url, create_tables=True)


@pytest.fixture
def rollback_service(audit_service, checkpoint_service) -> ScenarioRollbackService:
    return ScenarioRollbackService(
        audit_service=audit_service, checkpoint_service=checkpoint_service
    )


def make_baseline(
    checkpoint_service: ForecastCheckpointService, completed: bool = True
) -> str:
    """Create a baseline checkpoint, optionally completing it."""
    checkpoint = checkpoint_service.create(
        store_id="store_001",
        model_version="14",
        model_stage="Production",
        horizon_days=7,
        history_start_date=HISTORY_START,
        history_end_date=HISTORY_START + timedelta(days=29),
        forecast_start_date=HISTORY_START + timedelta(days=30),
        forecast_end_date=HISTORY_START + timedelta(days=36),
        data_source="kaggle_m5",
        dataset_version="v1.0.0",
        feature_schema_version="v1",
    )
    if completed:
        checkpoint_service.mark_completed(checkpoint.forecast_id)
    return checkpoint.forecast_id


def failure_kwargs(**overrides) -> Dict[str, Any]:
    payload = {
        "request_id": "req_1",
        "store_id": "store_001",
        "model_version": "14",
        "scenario_type": "promotion",
        "request_payload": REQUEST_PAYLOAD,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Audit table and basic recording
# --------------------------------------------------------------------------


def test_audit_table_matches_the_specification():
    assert ScenarioAuditEvent.__tablename__ == TABLE_NAME == "scenario_audit_events"

    columns = set(ScenarioAuditEvent.__table__.columns.keys())
    required = {
        "id",
        "request_id",
        "scenario_id",
        "baseline_forecast_id",
        "store_id",
        "model_version",
        "scenario_type",
        "status",
        "error_code",
        "error_message",
        "request_payload",
        "response_summary",
        "duration_ms",
        "created_at",
    }
    assert required <= columns

    table = ScenarioAuditEvent.__table__
    assert table.columns["id"].primary_key is True
    for nullable_column in (
        "scenario_id",
        "baseline_forecast_id",
        "error_code",
        "error_message",
        "response_summary",
        "duration_ms",
    ):
        assert table.columns[nullable_column].nullable is True, nullable_column
    assert table.columns["request_payload"].nullable is False


def test_all_status_values_are_persistable(audit_service):
    for status in ScenarioAuditStatus:
        event = audit_service.record(
            request_id=f"req_{status.value}",
            store_id="store_001",
            model_version="14",
            scenario_type="promotion",
            status=status,
            request_payload=REQUEST_PAYLOAD,
        )
        assert event.status == status.value


def test_required_status_values_exist():
    required = {
        "received",
        "validation_failed",
        "capability_check_failed",
        "baseline_created",
        "scenario_completed",
        "scenario_failed",
        "rolled_back",
    }
    assert required <= {status.value for status in ScenarioAuditStatus}


def test_received_event_stores_the_request_payload(audit_service):
    audit_service.record(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.RECEIVED,
        request_payload=REQUEST_PAYLOAD,
    )

    events = audit_service.get_by_request_id("req_1")
    assert len(events) == 1
    assert events[0].request_payload == REQUEST_PAYLOAD
    assert events[0].created_at is not None


def test_record_safely_swallows_database_failures(monkeypatch, audit_service):
    def explode(**_kwargs):
        raise DatabaseUnavailableError()

    monkeypatch.setattr(audit_service, "record", explode)

    # Must not raise: an audit failure may never mask the original error.
    assert audit_service.record_safely(request_id="req_1") is None


# --------------------------------------------------------------------------
# Validation failure creates an audit event
# --------------------------------------------------------------------------


def test_validation_failure_creates_audit_event(rollback_service, audit_service):
    exc = ServiceValidationError(
        ValidationErrorCode.HISTORY_TOO_SHORT,
        "history must contain at least 7 rows, received 3",
        field="history",
        details={"received": 3},
    )

    result = rollback_service.handle_validation_failure(exc=exc, **failure_kwargs())

    assert isinstance(result, RollbackResponse)
    assert result.status is ScenarioAuditStatus.VALIDATION_FAILED
    assert result.audit_event_recorded is True
    assert result.error_code == "SCENARIO_VALIDATION_ERROR"
    assert result.rolled_back is False

    events = audit_service.get_by_request_id("req_1")
    assert len(events) == 1
    assert events[0].status == "validation_failed"
    assert events[0].error_code == "SCENARIO_VALIDATION_ERROR"
    assert "at least 7 rows" in events[0].error_message
    assert events[0].scenario_id is None


def test_capability_failure_is_recorded_distinctly(rollback_service, audit_service):
    result = rollback_service.handle_capability_failure(
        exc=ServiceValidationError(
            ValidationErrorCode.UNSUPPORTED_SCENARIO_TYPE, "not enabled"
        ),
        **failure_kwargs(),
    )

    assert result.status is ScenarioAuditStatus.CAPABILITY_CHECK_FAILED
    assert audit_service.get_by_request_id("req_1")[0].status == "capability_check_failed"


# --------------------------------------------------------------------------
# Baseline creation failure
# --------------------------------------------------------------------------


def test_baseline_failure_marks_checkpoint_failed_and_creates_no_scenario(
    rollback_service, audit_service, checkpoint_service
):
    baseline_id = make_baseline(checkpoint_service, completed=False)

    result = rollback_service.handle_baseline_failure(
        exc=BaselineForecastError(
            BaselineForecastErrorCode.PREDICTION_FAILED, "model crashed"
        ),
        **failure_kwargs(baseline_forecast_id=baseline_id),
    )

    assert result.status is ScenarioAuditStatus.BASELINE_FAILED
    assert result.baseline_status is BaselineForecastStatus.FAILED
    assert result.baseline_preserved is False
    assert result.scenario_id is None

    assert checkpoint_service.get(baseline_id).status is BaselineForecastStatus.FAILED
    events = audit_service.get_by_request_id("req_1")
    assert events[0].scenario_id is None
    assert events[0].baseline_forecast_id == baseline_id


# --------------------------------------------------------------------------
# Model/prediction failure preserves a completed baseline
# --------------------------------------------------------------------------


def test_prediction_failure_preserves_completed_baseline(
    rollback_service, audit_service, checkpoint_service
):
    baseline_id = make_baseline(checkpoint_service, completed=True)

    result = rollback_service.handle_prediction_failure(
        exc=PredictionFailedError("the model blew up"),
        **failure_kwargs(scenario_id="scn_1", baseline_forecast_id=baseline_id),
    )

    assert result.status is ScenarioAuditStatus.SCENARIO_FAILED
    assert result.baseline_preserved is True
    assert result.baseline_status is BaselineForecastStatus.COMPLETED
    assert result.partial_results_discarded is True

    # The baseline is untouched and still reusable.
    checkpoint = checkpoint_service.get(baseline_id)
    assert checkpoint.status is BaselineForecastStatus.COMPLETED
    assert checkpoint.is_reusable is True
    assert checkpoint.error_code is None

    events = audit_service.get_by_request_id("req_1")
    assert events[0].status == "scenario_failed"
    assert events[0].scenario_id == "scn_1"


def test_feature_preparation_failure_preserves_completed_baseline(
    rollback_service, checkpoint_service
):
    baseline_id = make_baseline(checkpoint_service, completed=True)

    result = rollback_service.handle_feature_preparation_failure(
        exc=ServiceValidationError(ValidationErrorCode.INVALID_DATE_RANGE, "bad window"),
        **failure_kwargs(scenario_id="scn_2", baseline_forecast_id=baseline_id),
    )

    assert result.status is ScenarioAuditStatus.SCENARIO_FAILED
    assert result.baseline_preserved is True
    assert checkpoint_service.get(baseline_id).status is BaselineForecastStatus.COMPLETED


def test_persistence_failure_discards_partial_results(
    rollback_service, audit_service, checkpoint_service
):
    baseline_id = make_baseline(checkpoint_service, completed=True)

    result = rollback_service.handle_persistence_failure(
        exc=DatabaseUnavailableError(),
        **failure_kwargs(scenario_id="scn_3", baseline_forecast_id=baseline_id),
    )

    assert result.status is ScenarioAuditStatus.SCENARIO_FAILED
    assert result.partial_results_discarded is True
    assert result.baseline_preserved is True
    assert any("discarded" in note for note in result.notes)

    # No partial scenario result is ever stored as a response.
    events = audit_service.get_by_request_id("req_1")
    assert events[0].response_summary is None


def test_cancellation_marks_scenario_cancelled_and_releases_locks(
    rollback_service, audit_service, checkpoint_service
):
    baseline_id = make_baseline(checkpoint_service, completed=True)
    lock_key = ScenarioLockRegistry.build_key("store_001", "14", "promotion")
    rollback_service.lock_registry.acquire(lock_key, "req_1")
    assert rollback_service.lock_registry.is_held(lock_key)

    result = rollback_service.handle_cancellation(
        exc=TimeoutError("client went away"),
        **failure_kwargs(
            scenario_id="scn_4", baseline_forecast_id=baseline_id, lock_key=lock_key
        ),
    )

    assert result.status is ScenarioAuditStatus.SCENARIO_CANCELLED
    assert result.locks_released is True
    assert rollback_service.lock_registry.is_held(lock_key) is False
    assert result.baseline_preserved is True
    assert audit_service.get_by_request_id("req_1")[0].status == "scenario_cancelled"


def test_failure_handling_never_raises_when_the_database_is_down(
    monkeypatch, rollback_service
):
    def explode(*_args, **_kwargs):
        raise DatabaseUnavailableError()

    monkeypatch.setattr(rollback_service.audit_service, "record", explode)
    monkeypatch.setattr(rollback_service.checkpoint_service, "get", explode)

    result = rollback_service.handle_prediction_failure(
        exc=PredictionFailedError("boom"),
        **failure_kwargs(scenario_id="scn_5", baseline_forecast_id="bf_x"),
    )

    assert result.audit_event_recorded is False
    assert result.error_code == "PREDICTION_FAILED"


# --------------------------------------------------------------------------
# Rollback preserves history
# --------------------------------------------------------------------------


def test_rollback_preserves_audit_history(
    rollback_service, audit_service, checkpoint_service
):
    baseline_id = make_baseline(checkpoint_service, completed=True)
    audit_service.record(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.RECEIVED,
        request_payload=REQUEST_PAYLOAD,
        scenario_id="scn_9",
    )
    audit_service.record(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.SCENARIO_COMPLETED,
        request_payload=REQUEST_PAYLOAD,
        scenario_id="scn_9",
        baseline_forecast_id=baseline_id,
    )

    result = rollback_service.rollback_scenario("scn_9", reason="planner withdrew it")

    assert result.rolled_back is True
    assert result.status is ScenarioAuditStatus.ROLLED_BACK
    assert result.audit_history_preserved is True
    assert result.reason == "planner withdrew it"

    # Original events survive; the rollback is appended.
    events = audit_service.get_by_scenario_id("scn_9")
    statuses = [event.status for event in events]
    assert statuses == ["received", "scenario_completed", "rolled_back"]
    assert len(events) == 3


def test_rollback_does_not_delete_the_baseline_forecast(
    rollback_service, audit_service, checkpoint_service
):
    baseline_id = make_baseline(checkpoint_service, completed=True)
    audit_service.record(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.SCENARIO_COMPLETED,
        request_payload=REQUEST_PAYLOAD,
        scenario_id="scn_10",
        baseline_forecast_id=baseline_id,
    )

    result = rollback_service.rollback_scenario("scn_10", reason="superseded")

    assert result.baseline_forecast_id == baseline_id
    assert result.baseline_preserved is True
    checkpoint = checkpoint_service.get(baseline_id)
    assert checkpoint is not None
    assert checkpoint.status is BaselineForecastStatus.COMPLETED
    assert checkpoint.is_reusable is True


def test_rollback_reuses_identity_from_the_original_attempt(
    rollback_service, audit_service, checkpoint_service
):
    baseline_id = make_baseline(checkpoint_service, completed=True)
    audit_service.record(
        request_id="req_original",
        store_id="store_042",
        model_version="21",
        scenario_type="closure",
        status=ScenarioAuditStatus.SCENARIO_COMPLETED,
        request_payload=REQUEST_PAYLOAD,
        scenario_id="scn_11",
        baseline_forecast_id=baseline_id,
    )

    rollback_service.rollback_scenario("scn_11", reason="bad assumptions")

    events = audit_service.get_by_scenario_id("scn_11")
    rollback_event = events[-1]
    assert rollback_event.request_id == "req_original"
    assert rollback_event.store_id == "store_042"
    assert rollback_event.model_version == "21"
    assert rollback_event.scenario_type == "closure"
    assert rollback_event.request_payload == REQUEST_PAYLOAD


def test_rollback_of_unknown_scenario_is_still_recorded(rollback_service, audit_service):
    result = rollback_service.rollback_scenario("scn_unknown", reason="manual cleanup")

    assert result.rolled_back is True
    assert any("No prior audit events" in note for note in result.notes)
    assert audit_service.get_by_scenario_id("scn_unknown")[0].status == "rolled_back"


def test_rollback_reason_is_sanitised(rollback_service, audit_service):
    rollback_service.rollback_scenario(
        "scn_12", reason="failed reading /usr/local/airflow/secrets.yaml"
    )

    event = audit_service.get_by_scenario_id("scn_12")[0]
    assert "/usr/local/airflow" not in event.error_message
    assert "[redacted]" in event.error_message


def test_rollback_releases_locks(rollback_service):
    lock_key = ScenarioLockRegistry.build_key("store_001", "14", "promotion")
    rollback_service.lock_registry.acquire(lock_key, "req_1")

    result = rollback_service.rollback_scenario(
        "scn_13", reason="cancelled", request_id="req_1", lock_key=lock_key
    )

    assert result.locks_released is True
    assert rollback_service.lock_registry.is_held(lock_key) is False


# --------------------------------------------------------------------------
# Locks
# --------------------------------------------------------------------------


def test_concurrent_scenario_for_the_same_target_is_rejected():
    registry = ScenarioLockRegistry()
    key = ScenarioLockRegistry.build_key("store_001", "14", "promotion")

    registry.acquire(key, "req_1")

    with pytest.raises(ScenarioAlreadyRunningError) as exc_info:
        registry.acquire(key, "req_2")

    error = exc_info.value
    assert error.error_code == "SCENARIO_ALREADY_RUNNING"
    assert error.status_code == 409
    assert error.details["held_by_request_id"] == "req_1"


def test_same_request_may_reacquire_its_own_lock():
    registry = ScenarioLockRegistry()
    key = ScenarioLockRegistry.build_key("store_001", "14", "promotion")

    registry.acquire(key, "req_1")
    registry.acquire(key, "req_1")  # must not raise
    assert registry.release(key, "req_1") is True


def test_releasing_an_unheld_lock_is_safe():
    registry = ScenarioLockRegistry()
    assert registry.release("nothing:here:now") is False


def test_different_targets_do_not_block_each_other():
    registry = ScenarioLockRegistry()
    registry.acquire(ScenarioLockRegistry.build_key("store_001", "14", "promotion"), "r1")
    registry.acquire(ScenarioLockRegistry.build_key("store_002", "14", "promotion"), "r2")
    registry.acquire(ScenarioLockRegistry.build_key("store_001", "14", "closure"), "r3")


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_request_hash_is_order_independent():
    assert compute_request_hash({"a": 1, "b": 2}) == compute_request_hash({"b": 2, "a": 1})
    assert compute_request_hash({"a": 1}) != compute_request_hash({"a": 2})


def test_no_idempotency_key_means_no_replay(audit_service):
    assert audit_service.resolve_idempotent_request(None, REQUEST_PAYLOAD) is None


def test_unknown_idempotency_key_means_no_replay(audit_service):
    assert audit_service.resolve_idempotent_request("key-new", REQUEST_PAYLOAD) is None


def test_idempotent_retry_returns_the_same_scenario(audit_service):
    original = {"scenario_id": "scn_42", "impact": {"total_difference": 150.0}}
    audit_service.record(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.SCENARIO_COMPLETED,
        request_payload=REQUEST_PAYLOAD,
        scenario_id="scn_42",
        response_summary=original,
        idempotency_key="key-1",
    )

    replay = audit_service.resolve_idempotent_request("key-1", REQUEST_PAYLOAD)

    assert replay == original
    assert replay["scenario_id"] == "scn_42"


def test_idempotency_conflict_on_a_different_payload(audit_service):
    audit_service.record(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.SCENARIO_COMPLETED,
        request_payload=REQUEST_PAYLOAD,
        scenario_id="scn_42",
        response_summary={"scenario_id": "scn_42"},
        idempotency_key="key-1",
    )

    different = {**REQUEST_PAYLOAD, "horizon_days": 30}
    with pytest.raises(IdempotencyKeyConflictError) as exc_info:
        audit_service.resolve_idempotent_request("key-1", different)

    error = exc_info.value
    assert error.error_code == "IDEMPOTENCY_KEY_CONFLICT"
    assert error.status_code == 409
    assert error.field == "Idempotency-Key"
    assert error.details["original_request_id"] == "req_1"


def test_failed_attempt_under_a_key_allows_a_retry(audit_service):
    audit_service.record(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.SCENARIO_FAILED,
        request_payload=REQUEST_PAYLOAD,
        idempotency_key="key-retry",
        error_code="PREDICTION_FAILED",
    )

    # Same payload, previous attempt failed: the caller may try again.
    assert audit_service.resolve_idempotent_request("key-retry", REQUEST_PAYLOAD) is None


def test_idempotency_conflict_renders_as_the_standard_envelope():
    from api.middleware.error_handler import build_error_payload

    error = IdempotencyKeyConflictError(
        field="Idempotency-Key", details={"original_request_id": "req_1"}
    )

    payload = build_error_payload(error, request_id="req_2")

    assert payload["success"] is False
    assert payload["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert payload["error"]["field"] == "Idempotency-Key"


def test_completed_lookup_finds_only_completed_attempts(audit_service):
    audit_service.record(
        request_id="req_1",
        store_id="store_001",
        model_version="14",
        scenario_type="promotion",
        status=ScenarioAuditStatus.SCENARIO_FAILED,
        request_payload=REQUEST_PAYLOAD,
        idempotency_key="key-2",
    )

    assert audit_service.find_completed_by_idempotency_key("key-2") is None
    assert audit_service.find_any_by_idempotency_key("key-2") is not None
