"""Tests for scenario persistence and dataset lineage resolution."""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.core.errors import DatabaseUnavailableError  # noqa: E402
from api.db.models.scenario import (  # noqa: E402
    SCENARIO_DAILY_FORECASTS_TABLE,
    SCENARIO_FORECASTS_TABLE,
    ScenarioAuditEvent,
    ScenarioDailyForecast,
    ScenarioForecast,
    ScenarioForecastStatus,
)
from api.db.repositories.scenario_audit_repository import (  # noqa: E402
    FAILURE_STATUSES,
    ScenarioAuditRepository,
    ScenarioDurationStats,
)
from api.db.repositories.scenario_repository import ScenarioRepository  # noqa: E402
from api.schemas.scenario_comparison import (  # noqa: E402
    ForecastType,
    ScenarioComparisonResponse,
)
from api.services.dataset_lineage_service import (  # noqa: E402
    LINEAGE_PARTIAL_WARNING_TEMPLATE,
    LINEAGE_UNAVAILABLE_WARNING,
    DatasetLineageService,
    DatasetLineageStatus,
    LineageSource,
    ManifestDatasetRegistry,
    ScenarioDatasetLineage,
)
from api.services.scenario_audit_service import ScenarioAuditStatus  # noqa: E402

FORECAST_START = date(2026, 2, 1)
HORIZON_DAYS = 7
SCENARIO_ID = "scn_test_001"
BASELINE_ID = "bf_test_001"

REQUEST_PAYLOAD = {"store_id": "store_001", "horizon_days": HORIZON_DAYS}
RESPONSE_SUMMARY = {"scenario_id": SCENARIO_ID, "impact": {"total_difference": 150.0}}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def database_url(tmp_path) -> str:
    """File-backed SQLite so both repositories share one schema."""
    return f"sqlite+pysqlite:///{tmp_path / 'scenario_repo.db'}"


@pytest.fixture
def repo(database_url) -> ScenarioRepository:
    return ScenarioRepository(database_url=database_url, create_tables=True)


@pytest.fixture
def audit_repo(database_url, repo) -> ScenarioAuditRepository:
    return ScenarioAuditRepository(database_url=database_url, create_tables=True)


def scenario_kwargs(**overrides) -> Dict[str, Any]:
    payload = {
        "scenario_id": SCENARIO_ID,
        "store_id": "store_001",
        "model_version": "14",
        "model_stage": "Production",
        "scenario_type": "promotion",
        "horizon_days": HORIZON_DAYS,
        "forecast_type": ForecastType.PREDICTIVE_WHAT_IF.value,
        "request_payload": REQUEST_PAYLOAD,
        "baseline_forecast_id": BASELINE_ID,
    }
    payload.update(overrides)
    return payload


def daily_rows(count: int = HORIZON_DAYS) -> List[Dict[str, Any]]:
    return [
        {
            "forecast_date": FORECAST_START + timedelta(days=offset),
            "baseline_p10": 90.0,
            "baseline_p50": 100.0,
            "baseline_p90": 110.0,
            "scenario_p10": 108.0,
            "scenario_p50": 120.0,
            "scenario_p90": 132.0,
            "difference_p50": 20.0,
            "difference_pct_p50": 20.0,
        }
        for offset in range(count)
    ]


def audit_kwargs(**overrides) -> Dict[str, Any]:
    payload = {
        "request_id": "req_1",
        "store_id": "store_001",
        "model_version": "14",
        "scenario_type": "promotion",
        "status": ScenarioAuditStatus.RECEIVED,
        "request_payload": REQUEST_PAYLOAD,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def test_scenario_forecasts_table_matches_the_specification():
    assert ScenarioForecast.__tablename__ == SCENARIO_FORECASTS_TABLE == "scenario_forecasts"

    columns = set(ScenarioForecast.__table__.columns.keys())
    assert columns == {
        "id",
        "scenario_id",
        "baseline_forecast_id",
        "store_id",
        "model_version",
        "model_stage",
        "scenario_type",
        "horizon_days",
        "status",
        "forecast_type",
        "request_payload",
        "response_summary",
        "created_at",
        "completed_at",
        "expires_at",
        "error_code",
        "error_message",
        # Attribution, added with authentication: a saved scenario records who
        # created it, and keeps a readable email after the account is removed.
        "created_by_id",
        "created_by_email",
    }

    table = ScenarioForecast.__table__
    assert table.columns["id"].primary_key is True
    assert table.columns["scenario_id"].unique is True
    assert table.columns["completed_at"].nullable is True
    assert table.columns["request_payload"].nullable is False


def test_scenario_daily_forecasts_table_matches_the_specification():
    assert (
        ScenarioDailyForecast.__tablename__
        == SCENARIO_DAILY_FORECASTS_TABLE
        == "scenario_daily_forecasts"
    )

    columns = set(ScenarioDailyForecast.__table__.columns.keys())
    assert columns == {
        "id",
        "scenario_id",
        "forecast_date",
        "baseline_p10",
        "baseline_p50",
        "baseline_p90",
        "scenario_p10",
        "scenario_p50",
        "scenario_p90",
        "difference_p50",
        "difference_pct_p50",
        "created_at",
    }
    assert ScenarioDailyForecast.__table__.columns["id"].primary_key is True


def test_audit_table_is_reachable_from_the_models_package():
    from api.db.models import ScenarioAuditEvent as Exported

    assert Exported is ScenarioAuditEvent
    assert Exported.__tablename__ == "scenario_audit_events"


def test_all_scenario_tables_are_created_together(repo):
    from sqlalchemy import inspect

    tables = set(inspect(repo.engine).get_table_names())
    assert {
        "scenario_forecasts",
        "scenario_daily_forecasts",
        "scenario_audit_events",
        "scenario_baseline_forecasts",
    } <= tables


# --------------------------------------------------------------------------
# Scenario creation
# --------------------------------------------------------------------------


def test_create_scenario(repo):
    row = repo.create_scenario(**scenario_kwargs())

    assert row.scenario_id == SCENARIO_ID
    assert row.status == ScenarioForecastStatus.CREATED.value
    assert row.forecast_type == "predictive_what_if"
    assert row.baseline_forecast_id == BASELINE_ID
    assert row.request_payload == REQUEST_PAYLOAD
    assert row.response_summary is None
    assert row.completed_at is None
    assert row.created_at is not None
    assert row.id is not None


def test_created_scenario_is_readable(repo):
    repo.create_scenario(**scenario_kwargs())

    fetched = repo.get_scenario(SCENARIO_ID)

    assert fetched is not None
    assert fetched.scenario_id == SCENARIO_ID
    assert fetched.store_id == "store_001"
    assert fetched.is_retrievable is False  # not completed yet


def test_default_ttl_sets_expiry(repo):
    row = repo.create_scenario(**scenario_kwargs())

    assert row.expires_at is not None
    assert row.is_expired is False


def test_explicit_expiry_overrides_the_ttl(repo):
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    row = repo.create_scenario(**scenario_kwargs(expires_at=expires_at))

    assert row.expires_at is not None
    assert row.is_expired is False


def test_get_unknown_scenario_returns_none(repo):
    assert repo.get_scenario("scn_nope") is None


def test_duplicate_scenario_id_is_rejected(repo):
    repo.create_scenario(**scenario_kwargs())

    with pytest.raises(DatabaseUnavailableError):
        repo.create_scenario(**scenario_kwargs())


# --------------------------------------------------------------------------
# Status transitions
# --------------------------------------------------------------------------


def test_status_transition_to_completed_stamps_completed_at(repo):
    repo.create_scenario(**scenario_kwargs())

    updated = repo.update_scenario_status(
        SCENARIO_ID,
        ScenarioForecastStatus.COMPLETED,
        response_summary=RESPONSE_SUMMARY,
    )

    assert updated.status == "completed"
    assert updated.completed_at is not None
    assert updated.response_summary == RESPONSE_SUMMARY
    assert updated.is_retrievable is True


def test_status_transition_to_failed_records_the_error(repo):
    repo.create_scenario(**scenario_kwargs())

    updated = repo.update_scenario_status(
        SCENARIO_ID,
        ScenarioForecastStatus.FAILED,
        error_code="PREDICTION_FAILED",
        error_message="model crashed",
    )

    assert updated.status == "failed"
    assert updated.error_code == "PREDICTION_FAILED"
    assert updated.error_message == "model crashed"
    # A failure is never stamped as a completion.
    assert updated.completed_at is None
    assert updated.is_retrievable is False


@pytest.mark.parametrize("status", list(ScenarioForecastStatus))
def test_every_status_is_persistable(repo, status):
    repo.create_scenario(**scenario_kwargs(scenario_id=f"scn_{status.value}"))

    updated = repo.update_scenario_status(f"scn_{status.value}", status)

    assert updated.status == status.value
    assert repo.get_scenario(f"scn_{status.value}").status == status.value


def test_status_update_on_unknown_scenario_returns_none(repo):
    assert (
        repo.update_scenario_status("scn_nope", ScenarioForecastStatus.COMPLETED) is None
    )


def test_completed_scenario_retains_its_request_payload(repo):
    repo.create_scenario(**scenario_kwargs())
    repo.update_scenario_status(
        SCENARIO_ID, ScenarioForecastStatus.COMPLETED, response_summary=RESPONSE_SUMMARY
    )

    row = repo.get_scenario(SCENARIO_ID)
    assert row.request_payload == REQUEST_PAYLOAD
    assert row.response_summary == RESPONSE_SUMMARY


# --------------------------------------------------------------------------
# Daily forecast insertion
# --------------------------------------------------------------------------


def test_save_daily_forecasts(repo):
    repo.create_scenario(**scenario_kwargs())

    saved = repo.save_daily_forecasts(SCENARIO_ID, daily_rows())

    assert len(saved) == HORIZON_DAYS
    stored = repo.get_daily_forecasts(SCENARIO_ID)
    assert len(stored) == HORIZON_DAYS
    assert [row.forecast_date for row in stored] == [
        FORECAST_START + timedelta(days=offset) for offset in range(HORIZON_DAYS)
    ]
    first = stored[0]
    assert first.baseline_p50 == 100.0
    assert first.scenario_p50 == 120.0
    assert first.difference_p50 == 20.0
    assert first.difference_pct_p50 == 20.0
    assert first.created_at is not None


def test_daily_forecasts_accept_missing_quantiles(repo):
    repo.create_scenario(**scenario_kwargs())

    repo.save_daily_forecasts(
        SCENARIO_ID,
        [{"forecast_date": FORECAST_START, "baseline_p50": 100.0, "scenario_p50": 120.0}],
    )

    row = repo.get_daily_forecasts(SCENARIO_ID)[0]
    assert row.baseline_p10 is None
    assert row.scenario_p90 is None
    assert row.baseline_p50 == 100.0


def test_saving_daily_forecasts_twice_does_not_duplicate(repo):
    repo.create_scenario(**scenario_kwargs())
    repo.save_daily_forecasts(SCENARIO_ID, daily_rows())

    repo.save_daily_forecasts(SCENARIO_ID, daily_rows())

    assert len(repo.get_daily_forecasts(SCENARIO_ID)) == HORIZON_DAYS


def test_resaving_daily_forecasts_replaces_values(repo):
    repo.create_scenario(**scenario_kwargs())
    repo.save_daily_forecasts(SCENARIO_ID, daily_rows())

    revised = daily_rows()
    revised[0]["scenario_p50"] = 999.0
    repo.save_daily_forecasts(SCENARIO_ID, revised)

    assert repo.get_daily_forecasts(SCENARIO_ID)[0].scenario_p50 == 999.0


def test_saving_no_daily_forecasts_is_a_noop(repo):
    repo.create_scenario(**scenario_kwargs())

    assert repo.save_daily_forecasts(SCENARIO_ID, []) == []
    assert repo.get_daily_forecasts(SCENARIO_ID) == []


def test_daily_forecast_dates_accept_strings_and_datetimes(repo):
    repo.create_scenario(**scenario_kwargs())

    repo.save_daily_forecasts(
        SCENARIO_ID,
        [
            {"forecast_date": "2026-02-01", "baseline_p50": 1.0},
            {"forecast_date": datetime(2026, 2, 2, 12, 0), "baseline_p50": 2.0},
        ],
    )

    stored = repo.get_daily_forecasts(SCENARIO_ID)
    assert [row.forecast_date for row in stored] == [date(2026, 2, 1), date(2026, 2, 2)]


def test_daily_forecasts_are_reachable_from_the_scenario(repo):
    repo.create_scenario(**scenario_kwargs())
    repo.save_daily_forecasts(SCENARIO_ID, daily_rows(3))

    assert len(repo.get_scenario(SCENARIO_ID).daily_forecasts) == 3


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------


def test_rollback_marks_the_scenario_rolled_back(repo):
    repo.create_scenario(**scenario_kwargs())
    repo.update_scenario_status(
        SCENARIO_ID, ScenarioForecastStatus.COMPLETED, response_summary=RESPONSE_SUMMARY
    )

    rolled_back = repo.rollback_scenario(SCENARIO_ID, reason="assumptions were wrong")

    assert rolled_back.status == "rolled_back"
    assert rolled_back.error_message == "assumptions were wrong"


def test_rollback_preserves_the_row_and_its_daily_forecasts(repo):
    repo.create_scenario(**scenario_kwargs())
    repo.save_daily_forecasts(SCENARIO_ID, daily_rows())
    repo.update_scenario_status(
        SCENARIO_ID, ScenarioForecastStatus.COMPLETED, response_summary=RESPONSE_SUMMARY
    )

    repo.rollback_scenario(SCENARIO_ID, reason="withdrawn")

    row = repo.get_scenario(SCENARIO_ID)
    assert row is not None
    assert row.response_summary == RESPONSE_SUMMARY
    assert len(repo.get_daily_forecasts(SCENARIO_ID)) == HORIZON_DAYS
    # Withdrawn results are no longer served.
    assert row.is_retrievable is False


def test_rollback_reason_is_sanitised(repo):
    repo.create_scenario(**scenario_kwargs())

    row = repo.rollback_scenario(
        SCENARIO_ID, reason="bad config at /usr/local/airflow/secrets.yaml"
    )

    assert "/usr/local/airflow" not in row.error_message
    assert "[redacted]" in row.error_message


def test_rollback_of_unknown_scenario_returns_none(repo):
    assert repo.rollback_scenario("scn_nope", reason="n/a") is None


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


def test_expire_old_scenarios_marks_past_expiry_rows(repo):
    repo.create_scenario(
        **scenario_kwargs(
            scenario_id="scn_stale",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    repo.create_scenario(
        **scenario_kwargs(
            scenario_id="scn_fresh",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )

    assert repo.expire_old_scenarios() == 1
    assert repo.get_scenario("scn_stale").status == "expired"
    assert repo.get_scenario("scn_fresh").status == "created"


def test_expired_scenario_is_retrievable_as_a_record_but_not_servable(repo):
    repo.create_scenario(
        **scenario_kwargs(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    repo.update_scenario_status(
        SCENARIO_ID, ScenarioForecastStatus.COMPLETED, response_summary=RESPONSE_SUMMARY
    )

    row = repo.get_scenario(SCENARIO_ID)

    assert row is not None
    assert row.is_expired is True
    assert row.is_retrievable is False


def test_expiry_does_not_touch_failed_or_rolled_back_scenarios(repo):
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    repo.create_scenario(**scenario_kwargs(scenario_id="scn_failed", expires_at=stale))
    repo.update_scenario_status("scn_failed", ScenarioForecastStatus.FAILED)
    repo.create_scenario(**scenario_kwargs(scenario_id="scn_rb", expires_at=stale))
    repo.update_scenario_status("scn_rb", ScenarioForecastStatus.ROLLED_BACK)

    assert repo.expire_old_scenarios() == 0
    assert repo.get_scenario("scn_failed").status == "failed"
    assert repo.get_scenario("scn_rb").status == "rolled_back"


def test_scenarios_without_expiry_never_expire(repo):
    """An explicit ttl=None disables expiry; omitting it uses the default."""
    repo_no_ttl = ScenarioRepository(engine=repo.engine, ttl=None)
    repo_no_ttl.create_scenario(**scenario_kwargs(scenario_id="scn_forever"))

    row = repo_no_ttl.get_scenario("scn_forever")
    assert row.expires_at is None
    assert row.is_expired is False
    assert repo_no_ttl.expire_old_scenarios() == 0

    # The default-TTL repository still stamps an expiry.
    repo.create_scenario(**scenario_kwargs(scenario_id="scn_default_ttl"))
    assert repo.get_scenario("scn_default_ttl").expires_at is not None


def test_list_expired_scenarios(repo):
    repo.create_scenario(
        **scenario_kwargs(
            scenario_id="scn_stale",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    repo.expire_old_scenarios()

    expired = repo.list_expired_scenarios()
    assert [row.scenario_id for row in expired] == ["scn_stale"]


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


def test_list_scenarios_by_store(repo):
    repo.create_scenario(**scenario_kwargs(scenario_id="scn_a"))
    repo.create_scenario(**scenario_kwargs(scenario_id="scn_b"))
    repo.create_scenario(**scenario_kwargs(scenario_id="scn_c", store_id="store_002"))

    rows = repo.list_scenarios_by_store("store_001")

    assert {row.scenario_id for row in rows} == {"scn_a", "scn_b"}


def test_list_scenarios_by_store_filters_on_status(repo):
    repo.create_scenario(**scenario_kwargs(scenario_id="scn_a"))
    repo.create_scenario(**scenario_kwargs(scenario_id="scn_b"))
    repo.update_scenario_status("scn_b", ScenarioForecastStatus.COMPLETED)

    rows = repo.list_scenarios_by_store(
        "store_001", statuses=[ScenarioForecastStatus.COMPLETED]
    )

    assert [row.scenario_id for row in rows] == ["scn_b"]


def test_list_scenarios_respects_the_limit(repo):
    for index in range(5):
        repo.create_scenario(**scenario_kwargs(scenario_id=f"scn_{index}"))

    assert len(repo.list_scenarios_by_store("store_001", limit=2)) == 2


def test_list_for_unknown_store_is_empty(repo):
    assert repo.list_scenarios_by_store("store_nope") == []


# --------------------------------------------------------------------------
# Audit repository
# --------------------------------------------------------------------------


def test_create_audit_event(audit_repo):
    event = audit_repo.create_audit_event(**audit_kwargs(scenario_id=SCENARIO_ID))

    assert event.status == "received"
    assert event.scenario_id == SCENARIO_ID
    assert event.request_payload == REQUEST_PAYLOAD
    assert event.created_at is not None
    assert event.id is not None


def test_audit_event_error_message_is_sanitised(audit_repo):
    event = audit_repo.create_audit_event(
        **audit_kwargs(
            status=ScenarioAuditStatus.SCENARIO_FAILED,
            error_code="PREDICTION_FAILED",
            error_message="crashed reading /usr/local/airflow/x.pkl",
        )
    )

    assert "/usr/local/airflow" not in event.error_message
    assert "[redacted]" in event.error_message


def test_get_audit_events_by_scenario_is_ordered(audit_repo):
    audit_repo.create_audit_event(**audit_kwargs(scenario_id=SCENARIO_ID))
    audit_repo.create_audit_event(
        **audit_kwargs(
            scenario_id=SCENARIO_ID, status=ScenarioAuditStatus.SCENARIO_COMPLETED
        )
    )
    audit_repo.create_audit_event(
        **audit_kwargs(scenario_id="scn_other", status=ScenarioAuditStatus.RECEIVED)
    )

    events = audit_repo.get_audit_events_by_scenario(SCENARIO_ID)

    assert [event.status for event in events] == ["received", "scenario_completed"]


def test_get_audit_events_for_unknown_scenario_is_empty(audit_repo):
    assert audit_repo.get_audit_events_by_scenario("scn_nope") == []


def test_get_recent_failures(audit_repo):
    audit_repo.create_audit_event(
        **audit_kwargs(status=ScenarioAuditStatus.SCENARIO_COMPLETED)
    )
    audit_repo.create_audit_event(
        **audit_kwargs(
            scenario_id="scn_f1",
            status=ScenarioAuditStatus.SCENARIO_FAILED,
            error_code="PREDICTION_FAILED",
        )
    )
    audit_repo.create_audit_event(
        **audit_kwargs(
            scenario_id="scn_f2",
            status=ScenarioAuditStatus.VALIDATION_FAILED,
            error_code="SCENARIO_VALIDATION_ERROR",
        )
    )

    failures = audit_repo.get_recent_failures()

    assert {event.scenario_id for event in failures} == {"scn_f1", "scn_f2"}
    assert all(event.status in FAILURE_STATUSES for event in failures)


def test_get_recent_failures_filters_by_store_and_time(audit_repo):
    audit_repo.create_audit_event(
        **audit_kwargs(status=ScenarioAuditStatus.SCENARIO_FAILED)
    )
    audit_repo.create_audit_event(
        **audit_kwargs(store_id="store_002", status=ScenarioAuditStatus.SCENARIO_FAILED)
    )

    assert len(audit_repo.get_recent_failures(store_id="store_001")) == 1
    assert (
        len(
            audit_repo.get_recent_failures(
                since=datetime.now(timezone.utc) + timedelta(minutes=1)
            )
        )
        == 0
    )


def test_get_scenario_duration_stats(audit_repo):
    for duration in (100, 200, 300):
        audit_repo.create_audit_event(
            **audit_kwargs(
                status=ScenarioAuditStatus.SCENARIO_COMPLETED, duration_ms=duration
            )
        )
    audit_repo.create_audit_event(
        **audit_kwargs(status=ScenarioAuditStatus.SCENARIO_FAILED, duration_ms=50)
    )

    stats = audit_repo.get_scenario_duration_stats()

    assert isinstance(stats, ScenarioDurationStats)
    assert stats.sample_count == 4
    assert stats.completed_count == 3
    assert stats.failure_count == 1
    assert stats.avg_duration_ms == pytest.approx(162.5)
    assert stats.min_duration_ms == 50
    assert stats.max_duration_ms == 300
    assert stats.success_rate == pytest.approx(0.75)


def test_duration_stats_ignore_untimed_rows(audit_repo):
    audit_repo.create_audit_event(
        **audit_kwargs(status=ScenarioAuditStatus.SCENARIO_COMPLETED, duration_ms=200)
    )
    # A validation failure is recorded before timing starts.
    audit_repo.create_audit_event(**audit_kwargs(status=ScenarioAuditStatus.VALIDATION_FAILED))

    stats = audit_repo.get_scenario_duration_stats()

    assert stats.sample_count == 1
    assert stats.avg_duration_ms == pytest.approx(200.0)
    assert stats.failure_count == 1


def test_duration_stats_on_an_empty_table(audit_repo):
    stats = audit_repo.get_scenario_duration_stats()

    assert stats.sample_count == 0
    assert stats.avg_duration_ms is None
    assert stats.success_rate is None


# --------------------------------------------------------------------------
# Dataset lineage
# --------------------------------------------------------------------------


class FakeRunMetadataSource:
    """Returns canned MLflow params, or raises."""

    def __init__(
        self, params: Optional[Dict[str, str]] = None, error: Optional[Exception] = None
    ) -> None:
        self.params = params or {}
        self.error = error
        self.calls: List[str] = []

    def get_run_params(self, model_version: str) -> Dict[str, str]:
        self.calls.append(model_version)
        if self.error:
            raise self.error
        return dict(self.params)


class FakeRegistry:
    """Returns a canned manifest lineage block."""

    def __init__(self, manifest: Optional[Dict[str, Any]] = None) -> None:
        self.manifest = manifest

    def get_manifest(self, raw_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self.manifest


FULL_PARAMS = {
    "data.source": "kaggle_m5",
    "data.version": "v1.0.0",
    "data.hash": "abc123",
    "data.raw_path": "/usr/local/airflow/data/raw/kaggle_m5/v1.0.0",
    "data.processed_path": "/usr/local/airflow/data/processed/kaggle_m5/v1.0.0",
    "data.date_min": "2011-01-29",
    "data.date_max": "2016-04-24",
    "data.schema_version": "v1",
    "data.ingestion_run_id": "ingest_42",
}

# What training actually logs today: no processed path, no ingestion run id.
TODAYS_PARAMS = {
    key: value
    for key, value in FULL_PARAMS.items()
    if key not in ("data.processed_path", "data.ingestion_run_id")
}


def lineage_service(params=None, manifest=None, error=None) -> DatasetLineageService:
    return DatasetLineageService(
        run_metadata_source=FakeRunMetadataSource(params, error),
        dataset_registry=FakeRegistry(manifest),
    )


def test_lineage_is_read_from_training_run_metadata():
    lineage = lineage_service(FULL_PARAMS).get_lineage("14")

    assert isinstance(lineage, ScenarioDatasetLineage)
    assert lineage.lineage_status is DatasetLineageStatus.AVAILABLE
    assert lineage.source_of_record is LineageSource.TRAINING_RUN
    assert lineage.data_source == "kaggle_m5"
    assert lineage.dataset_version == "v1.0.0"
    assert lineage.dataset_hash == "abc123"
    assert lineage.raw_data_path.endswith("raw/kaggle_m5/v1.0.0")
    assert lineage.processed_data_path.endswith("processed/kaggle_m5/v1.0.0")
    assert lineage.train_start_date == date(2011, 1, 29)
    assert lineage.train_end_date == date(2016, 4, 24)
    assert lineage.feature_schema_version == "v1"
    assert lineage.ingestion_run_id == "ingest_42"
    assert lineage.warnings == []
    assert lineage.missing_fields == []


def test_lineage_is_never_hardcoded():
    """A different training run yields different lineage."""
    other = {**FULL_PARAMS, "data.source": "store_sales", "data.version": "v2.1.0"}

    lineage = lineage_service(other).get_lineage("21")

    assert lineage.data_source == "store_sales"
    assert lineage.dataset_version == "v2.1.0"


def test_lineage_from_todays_training_params_is_partial():
    """Training does not log processed_path or ingestion_run_id yet."""
    lineage = lineage_service(TODAYS_PARAMS).get_lineage("14")

    assert lineage.lineage_status is DatasetLineageStatus.PARTIAL
    assert lineage.data_source == "kaggle_m5"
    assert set(lineage.missing_fields) == {"processed_data_path", "ingestion_run_id"}
    assert lineage.warnings == [
        LINEAGE_PARTIAL_WARNING_TEMPLATE.format(
            fields=", ".join(lineage.missing_fields)
        )
    ]


def test_registry_manifest_fills_gaps_left_by_the_run():
    manifest = {
        "data_source": "kaggle_m5",
        "dataset_version": "v1.0.0",
        "dataset_hash": "abc123",
        "processed_path": "/data/processed/kaggle_m5/v1.0.0",
        "ingestion_run_id": "ingest_99",
    }

    lineage = lineage_service(TODAYS_PARAMS, manifest=manifest).get_lineage("14")

    assert lineage.lineage_status is DatasetLineageStatus.AVAILABLE
    assert lineage.source_of_record is LineageSource.TRAINING_RUN_AND_REGISTRY
    assert lineage.ingestion_run_id == "ingest_99"
    assert lineage.warnings == []


def test_registry_alone_can_supply_lineage():
    manifest = {
        "data_source": "synthetic",
        "dataset_version": "v1.0.0",
        "dataset_hash": "deadbeef",
        "date_range_start": "2021-01-01",
        "date_range_end": "2021-12-31",
        "schema_version": "v1",
        "raw_path": "/data/raw/synthetic/v1.0.0",
    }

    lineage = lineage_service(params={}, manifest=manifest).get_lineage("14")

    assert lineage.source_of_record is LineageSource.DATASET_REGISTRY
    assert lineage.data_source == "synthetic"
    assert lineage.train_start_date == date(2021, 1, 1)
    assert lineage.lineage_status is DatasetLineageStatus.PARTIAL


def test_missing_lineage_warns_without_failing():
    lineage = lineage_service(params={}, manifest=None).get_lineage("14")

    assert lineage.lineage_status is DatasetLineageStatus.UNAVAILABLE
    assert lineage.lineage_status.value == "unavailable"
    assert lineage.warnings == [LINEAGE_UNAVAILABLE_WARNING]
    assert lineage.warnings == [
        "Dataset lineage metadata is unavailable for this model version."
    ]
    assert lineage.data_source is None
    assert lineage.source_of_record is LineageSource.NONE
    assert lineage.is_available is False


def test_lineage_source_failure_does_not_raise():
    lineage = lineage_service(error=RuntimeError("mlflow down")).get_lineage("14")

    assert lineage.lineage_status is DatasetLineageStatus.UNAVAILABLE
    assert lineage.warnings == [LINEAGE_UNAVAILABLE_WARNING]


def test_unparseable_dates_are_treated_as_missing():
    params = {**FULL_PARAMS, "data.date_min": "not-a-date"}

    lineage = lineage_service(params).get_lineage("14")

    assert lineage.train_start_date is None
    assert "train_start_date" in lineage.missing_fields
    assert lineage.lineage_status is DatasetLineageStatus.PARTIAL


def test_lineage_is_cached_per_model_version():
    source = FakeRunMetadataSource(FULL_PARAMS)
    service = DatasetLineageService(
        run_metadata_source=source, dataset_registry=FakeRegistry()
    )

    service.get_lineage("14")
    service.get_lineage("14")

    assert source.calls == ["14"]


def test_unavailable_lineage_is_not_cached():
    source = FakeRunMetadataSource({})
    service = DatasetLineageService(
        run_metadata_source=source, dataset_registry=FakeRegistry()
    )

    service.get_lineage("14")
    service.get_lineage("14")

    # Lineage appears as soon as training starts recording it, with no restart.
    assert source.calls == ["14", "14"]


def test_manifest_registry_reads_a_real_file(tmp_path):
    manifest = {
        "config_fingerprint": {"source": "kaggle_m5"},
        "lineage": {"data_source": "kaggle_m5", "dataset_version": "v1.0.0"},
    }
    (tmp_path / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    registry = ManifestDatasetRegistry(base_path=str(tmp_path))

    assert registry.get_manifest()["data_source"] == "kaggle_m5"


def test_manifest_registry_tolerates_a_missing_or_broken_file(tmp_path):
    assert ManifestDatasetRegistry(base_path=str(tmp_path)).get_manifest() is None

    (tmp_path / "dataset_manifest.json").write_text("{not json", encoding="utf-8")
    assert ManifestDatasetRegistry(base_path=str(tmp_path)).get_manifest() is None


# --------------------------------------------------------------------------
# Attaching lineage to a response
# --------------------------------------------------------------------------


def build_comparison() -> ScenarioComparisonResponse:
    from api.schemas.baseline_forecast import BaselineDailyForecast, ModelStage
    from api.schemas.scenario import ScenarioType
    from api.schemas.scenario_comparison import ForecastSeriesSummary
    from api.services.scenario_impact_service import ScenarioImpactService

    baseline = [
        BaselineDailyForecast(
            date=FORECAST_START, predicted_sales=100.0, lower_bound=90.0, upper_bound=110.0
        )
    ]
    scenario = [
        BaselineDailyForecast(
            date=FORECAST_START, predicted_sales=120.0, lower_bound=108.0, upper_bound=132.0
        )
    ]
    return ScenarioComparisonResponse(
        scenario_id=SCENARIO_ID,
        baseline_forecast_id=BASELINE_ID,
        scenario_forecast_id="sf_1",
        model_version="14",
        model_stage=ModelStage.PRODUCTION.value,
        scenario_type=ScenarioType.PROMOTION,
        baseline=ForecastSeriesSummary(horizon_days=1),
        scenario=ForecastSeriesSummary(horizon_days=1),
        impact=ScenarioImpactService().compute_impact(baseline, scenario),
        warnings=["pre-existing warning"],
    )


def test_lineage_is_attached_to_a_comparison_response():
    service = lineage_service(FULL_PARAMS)

    attached = service.attach_lineage(build_comparison())

    assert attached.dataset_lineage["data_source"] == "kaggle_m5"
    assert attached.dataset_lineage["dataset_hash"] == "abc123"
    assert attached.dataset_lineage["lineage_status"] == "available"
    # The original response object is not mutated.
    assert build_comparison().dataset_lineage is None


def test_missing_lineage_warning_reaches_the_response():
    service = lineage_service(params={})

    attached = service.attach_lineage(build_comparison())

    assert attached.dataset_lineage["lineage_status"] == "unavailable"
    assert LINEAGE_UNAVAILABLE_WARNING in attached.warnings
    # Existing warnings survive.
    assert "pre-existing warning" in attached.warnings


def test_attached_response_still_serialises():
    service = lineage_service(FULL_PARAMS)

    payload = service.attach_lineage(build_comparison()).model_dump(mode="json")

    assert payload["dataset_lineage"]["dataset_version"] == "v1.0.0"
    assert payload["dataset_lineage"]["train_start_date"] == "2011-01-29"
    restored = ScenarioComparisonResponse.model_validate(payload)
    assert restored.dataset_lineage["data_source"] == "kaggle_m5"


def test_lineage_uses_the_response_model_version_by_default():
    source = FakeRunMetadataSource(FULL_PARAMS)
    service = DatasetLineageService(
        run_metadata_source=source, dataset_registry=FakeRegistry()
    )

    service.attach_lineage(build_comparison())

    assert source.calls == ["14"]
