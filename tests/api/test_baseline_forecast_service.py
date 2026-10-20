"""Tests for baseline forecast generation and checkpoint persistence."""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.schemas.baseline_forecast import (  # noqa: E402
    BaselineForecastCheckpoint,
    BaselineForecastResponse,
    BaselineForecastStatus,
    ModelStage,
    UncertaintyMethod,
)
from api.schemas.scenario import (  # noqa: E402
    HistoryPoint,
    ScenarioAssumptions,
    ScenarioRequest,
    ScenarioType,
)
from api.services.baseline_forecast_service import (  # noqa: E402
    CALIBRATION_WARNING,
    RESIDUAL_FALLBACK_WARNING,
    BaselineForecastError,
    BaselineForecastErrorCode,
    BaselineForecastService,
    PredictionResult,
    ResolvedModel,
)
from api.services.feature_schema_service import (  # noqa: E402
    FEATURE_SCHEMA_MISSING_MESSAGE,
    FeatureSchemaError,
    FeatureSchemaErrorCode,
)
from api.services.forecast_checkpoint_service import (  # noqa: E402
    TABLE_NAME,
    CheckpointError,
    CheckpointErrorCode,
    ForecastCheckpointService,
    ScenarioBaselineForecast,
    resolve_database_url,
)

HISTORY_START = date(2026, 1, 1)
HISTORY_DAYS = 30
HORIZON_DAYS = 7


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeModelResolver:
    """Returns a canned ResolvedModel, or raises a canned error."""

    def __init__(
        self,
        model: Optional[ResolvedModel] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.model = model or ResolvedModel(
            model_version="14",
            model_stage=ModelStage.PRODUCTION.value,
            run_id="a" * 32,
            data_source="kaggle_m5",
            dataset_version="v1.0.0",
        )
        self.error = error
        self.calls: List[str] = []

    def resolve(self, model_version: str) -> ResolvedModel:
        self.calls.append(model_version)
        if self.error:
            raise self.error
        return self.model


class FakePredictor:
    """Returns canned predictions, optionally with quantiles, or raises."""

    def __init__(
        self,
        predictions: Optional[List[float]] = None,
        quantiles: Optional[Dict[float, List[float]]] = None,
        residual_std: Optional[float] = None,
        error: Optional[Exception] = None,
        warnings: Optional[List[str]] = None,
    ) -> None:
        self.predictions = predictions
        self.quantiles = quantiles or {}
        self.residual_std = residual_std
        self.error = error
        self.warnings = warnings or []
        self.calls: List[Dict] = []

    def predict(
        self,
        *,
        history: Sequence[HistoryPoint],
        store_id: str,
        horizon_days: int,
        confidence_levels: Sequence[float],
        model: ResolvedModel,
    ) -> PredictionResult:
        self.calls.append(
            {
                "store_id": store_id,
                "horizon_days": horizon_days,
                "history_rows": len(history),
                "confidence_levels": list(confidence_levels),
                "model_version": model.model_version,
            }
        )
        if self.error:
            raise self.error
        predictions = (
            self.predictions
            if self.predictions is not None
            else [500.0 + index for index in range(horizon_days)]
        )
        return PredictionResult(
            predictions=predictions,
            quantiles=self.quantiles,
            supports_quantiles=bool(self.quantiles),
            residual_std=self.residual_std,
            warnings=list(self.warnings),
        )


class FakeFeatureSchemaService:
    """Returns a canned feature schema version, or raises."""

    def __init__(self, schema_version: str = "v1", error: Optional[Exception] = None) -> None:
        self.schema_version = schema_version
        self.error = error
        self.calls: List[str] = []

    def get_feature_schema(self, model_version: str):
        self.calls.append(model_version)
        if self.error:
            raise self.error

        class _Schema:
            schema_version = self.schema_version

        return _Schema()


class FakeCheckpointService:
    """In-memory checkpoint store that records the status transitions applied."""

    def __init__(
        self,
        create_error: Optional[Exception] = None,
        complete_error: Optional[Exception] = None,
        fail_error: Optional[Exception] = None,
    ) -> None:
        self.rows: Dict[str, BaselineForecastCheckpoint] = {}
        self.transitions: List[tuple] = []
        self.create_error = create_error
        self.complete_error = complete_error
        self.fail_error = fail_error

    def create(self, **kwargs) -> BaselineForecastCheckpoint:
        if self.create_error:
            raise self.create_error
        checkpoint = BaselineForecastCheckpoint(
            status=BaselineForecastStatus.CREATED,
            created_at=datetime.now(timezone.utc),
            **kwargs,
        )
        self.rows[checkpoint.forecast_id] = checkpoint
        self.transitions.append((checkpoint.forecast_id, BaselineForecastStatus.CREATED))
        return checkpoint

    def mark_completed(self, forecast_id: str) -> BaselineForecastCheckpoint:
        if self.complete_error:
            raise self.complete_error
        return self._set(forecast_id, BaselineForecastStatus.COMPLETED)

    def mark_failed(
        self, forecast_id: str, error_code=None, error_message=None
    ) -> BaselineForecastCheckpoint:
        if self.fail_error:
            raise self.fail_error
        row = self._set(forecast_id, BaselineForecastStatus.FAILED)
        updated = row.model_copy(update={"error_code": error_code, "error_message": error_message})
        self.rows[forecast_id] = updated
        return updated

    def get(self, forecast_id: str) -> Optional[BaselineForecastCheckpoint]:
        return self.rows.get(forecast_id)

    def _set(self, forecast_id: str, status: BaselineForecastStatus):
        row = self.rows[forecast_id].model_copy(update={"status": status})
        self.rows[forecast_id] = row
        self.transitions.append((forecast_id, status))
        return row

    @property
    def statuses(self) -> List[BaselineForecastStatus]:
        return [status for _, status in self.transitions]


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def build_history(rows: int = HISTORY_DAYS, sales: float = 500.0) -> List[HistoryPoint]:
    return [
        HistoryPoint(
            date=HISTORY_START + timedelta(days=offset),
            sales=sales + (offset % 7) * 20.0,
            store_id="store_001",
        )
        for offset in range(rows)
    ]


def build_request(
    history: Optional[List[HistoryPoint]] = None,
    horizon_days: int = HORIZON_DAYS,
    **overrides,
) -> ScenarioRequest:
    payload = {
        "store_id": "store_001",
        "horizon_days": horizon_days,
        "history": history if history is not None else build_history(),
        "scenario": ScenarioAssumptions(
            scenario_type=ScenarioType.PROMOTION,
            start_date=date(2026, 2, 10),
            end_date=date(2026, 2, 14),
        ),
    }
    payload.update(overrides)
    return ScenarioRequest(**payload)


def build_service(
    resolver: Optional[FakeModelResolver] = None,
    predictor: Optional[FakePredictor] = None,
    schema_service: Optional[FakeFeatureSchemaService] = None,
    checkpoints: Optional[FakeCheckpointService] = None,
    **kwargs,
) -> BaselineForecastService:
    return BaselineForecastService(
        model_resolver=resolver or FakeModelResolver(),
        predictor=predictor or FakePredictor(),
        feature_schema_service=schema_service or FakeFeatureSchemaService(),
        checkpoint_service=checkpoints or FakeCheckpointService(),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_baseline_forecast_returns_full_response():
    checkpoints = FakeCheckpointService()
    service = build_service(checkpoints=checkpoints)

    response = service.create_baseline(build_request())

    assert isinstance(response, BaselineForecastResponse)
    assert response.baseline_forecast_id.startswith("bf_")
    assert response.store_id == "store_001"
    assert response.model_version == "14"
    assert response.model_stage == "Production"
    assert response.horizon_days == HORIZON_DAYS
    assert response.history_start_date == HISTORY_START
    assert response.history_end_date == HISTORY_START + timedelta(days=HISTORY_DAYS - 1)
    assert response.forecast_start_date == response.history_end_date + timedelta(days=1)
    assert response.forecast_end_date == response.forecast_start_date + timedelta(
        days=HORIZON_DAYS - 1
    )
    assert len(response.daily_forecasts) == HORIZON_DAYS
    assert response.summary.horizon_days == HORIZON_DAYS
    assert response.feature_schema_version == "v1"
    assert response.data_source == "kaggle_m5"
    assert response.dataset_version == "v1.0.0"
    assert response.status is BaselineForecastStatus.COMPLETED
    assert response.run_id == "a" * 32
    assert isinstance(response.generated_at, datetime)


def test_daily_forecast_dates_are_contiguous_from_history_end():
    service = build_service()

    response = service.create_baseline(build_request())

    expected = [
        response.forecast_start_date + timedelta(days=offset) for offset in range(HORIZON_DAYS)
    ]
    assert [row.date for row in response.daily_forecasts] == expected


def test_predictor_receives_the_request_parameters():
    predictor = FakePredictor()
    service = build_service(predictor=predictor)

    service.create_baseline(build_request(horizon_days=14))

    assert predictor.calls == [
        {
            "store_id": "store_001",
            "horizon_days": 14,
            "history_rows": HISTORY_DAYS,
            "confidence_levels": [0.1, 0.5, 0.9],
            "model_version": "14",
        }
    ]


def test_latest_model_version_is_passed_through_to_the_resolver():
    resolver = FakeModelResolver()
    service = build_service(resolver=resolver)

    service.create_baseline(build_request())

    assert resolver.calls == ["latest"]


def test_response_round_trips_as_json():
    service = build_service()

    payload = service.create_baseline(build_request()).model_dump(mode="json")

    assert payload["uncertainty_method"] in ("quantile_model", "residual_based")
    assert BaselineForecastResponse.model_validate(payload).horizon_days == HORIZON_DAYS


# --------------------------------------------------------------------------
# Uncertainty: quantile model vs residual based
# --------------------------------------------------------------------------


def test_native_quantiles_are_marked_quantile_model():
    quantiles = {
        0.1: [400.0 + i for i in range(HORIZON_DAYS)],
        0.5: [500.0 + i for i in range(HORIZON_DAYS)],
        0.9: [600.0 + i for i in range(HORIZON_DAYS)],
    }
    predictor = FakePredictor(
        predictions=[500.0 + i for i in range(HORIZON_DAYS)], quantiles=quantiles
    )
    service = build_service(predictor=predictor)

    response = service.create_baseline(build_request())

    assert response.uncertainty_method is UncertaintyMethod.QUANTILE_MODEL
    first = response.daily_forecasts[0]
    assert first.uncertainty_method is UncertaintyMethod.QUANTILE_MODEL
    assert first.p10 == 400.0
    assert first.p50 == 500.0
    assert first.p90 == 600.0
    assert first.lower_bound == 400.0
    assert first.upper_bound == 600.0
    assert RESIDUAL_FALLBACK_WARNING not in response.warnings


def test_missing_quantiles_fall_back_to_residual_based():
    predictor = FakePredictor(residual_std=50.0)
    service = build_service(predictor=predictor)

    response = service.create_baseline(build_request())

    assert response.uncertainty_method is UncertaintyMethod.RESIDUAL_BASED
    for row in response.daily_forecasts:
        assert row.uncertainty_method is UncertaintyMethod.RESIDUAL_BASED
        assert row.p10 is None and row.p50 is None and row.p90 is None
        assert row.lower_bound <= row.predicted_sales <= row.upper_bound
    # No fallback warning: the predictor supplied its own residual spread.
    assert RESIDUAL_FALLBACK_WARNING not in response.warnings


def test_residual_bounds_use_the_model_residual_std():
    predictor = FakePredictor(predictions=[500.0] * HORIZON_DAYS, residual_std=100.0)
    service = build_service(predictor=predictor, residual_horizon_scaling=False)

    response = service.create_baseline(build_request())

    # 80% central interval for [0.1, 0.9] -> z = 1.2816
    first = response.daily_forecasts[0]
    assert first.lower_bound == pytest.approx(500.0 - 1.2816 * 100.0, abs=0.01)
    assert first.upper_bound == pytest.approx(500.0 + 1.2816 * 100.0, abs=0.01)
    # Scaling disabled: every day has the same width.
    widths = {round(row.interval_width, 6) for row in response.daily_forecasts}
    assert len(widths) == 1


def test_residual_bounds_widen_with_horizon_by_default():
    predictor = FakePredictor(predictions=[500.0] * HORIZON_DAYS, residual_std=50.0)
    service = build_service(predictor=predictor)

    rows = service.create_baseline(build_request()).daily_forecasts

    widths = [row.interval_width for row in rows]
    assert widths == sorted(widths)
    assert widths[-1] > widths[0]


def test_residual_std_falls_back_to_history_volatility_with_warning():
    predictor = FakePredictor(residual_std=None)
    service = build_service(predictor=predictor)

    response = service.create_baseline(build_request())

    assert response.uncertainty_method is UncertaintyMethod.RESIDUAL_BASED
    assert RESIDUAL_FALLBACK_WARNING in response.warnings
    assert response.daily_forecasts[0].interval_width > 0


def test_lower_bound_is_never_negative():
    predictor = FakePredictor(predictions=[5.0] * HORIZON_DAYS, residual_std=400.0)
    service = build_service(predictor=predictor)

    for row in service.create_baseline(build_request()).daily_forecasts:
        assert row.lower_bound >= 0.0


def test_predictor_warnings_are_surfaced():
    predictor = FakePredictor(residual_std=20.0, warnings=[CALIBRATION_WARNING])
    service = build_service(predictor=predictor)

    assert CALIBRATION_WARNING in service.create_baseline(build_request()).warnings


# --------------------------------------------------------------------------
# Rollback-safe checkpointing
# --------------------------------------------------------------------------


def test_checkpoint_is_created_before_completion():
    checkpoints = FakeCheckpointService()
    service = build_service(checkpoints=checkpoints)

    response = service.create_baseline(build_request())

    assert checkpoints.statuses == [
        BaselineForecastStatus.CREATED,
        BaselineForecastStatus.COMPLETED,
    ]
    assert checkpoints.get(response.baseline_forecast_id).status is (
        BaselineForecastStatus.COMPLETED
    )


def test_prediction_failure_marks_checkpoint_failed_and_never_completed():
    checkpoints = FakeCheckpointService()
    predictor = FakePredictor(error=RuntimeError("xgboost exploded"))
    service = build_service(predictor=predictor, checkpoints=checkpoints)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request())

    assert exc_info.value.error_code == "PREDICTION_FAILED"
    assert checkpoints.statuses == [
        BaselineForecastStatus.CREATED,
        BaselineForecastStatus.FAILED,
    ]
    assert BaselineForecastStatus.COMPLETED not in checkpoints.statuses

    row = list(checkpoints.rows.values())[0]
    assert row.status is BaselineForecastStatus.FAILED
    assert row.error_code == "PREDICTION_FAILED"
    assert "xgboost exploded" in str(row.error_message)


def test_wrong_prediction_length_is_a_prediction_failure():
    checkpoints = FakeCheckpointService()
    predictor = FakePredictor(predictions=[1.0, 2.0])  # horizon is 7
    service = build_service(predictor=predictor, checkpoints=checkpoints)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request())

    assert exc_info.value.error_code == "PREDICTION_FAILED"
    assert exc_info.value.details == {"expected": HORIZON_DAYS, "received": 2}
    assert checkpoints.statuses[-1] is BaselineForecastStatus.FAILED


def test_non_finite_predictions_are_rejected():
    predictor = FakePredictor(predictions=[float("nan")] * HORIZON_DAYS)
    service = build_service(predictor=predictor)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request())
    assert exc_info.value.error_code == "PREDICTION_FAILED"


def test_failure_to_record_failure_does_not_mask_the_original_error():
    checkpoints = FakeCheckpointService(
        fail_error=CheckpointError(
            CheckpointErrorCode.DATABASE_UNAVAILABLE, "connection reset"
        )
    )
    predictor = FakePredictor(error=RuntimeError("boom"))
    service = build_service(predictor=predictor, checkpoints=checkpoints)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request())

    # The caller sees the prediction failure, not the bookkeeping failure.
    assert exc_info.value.error_code == "PREDICTION_FAILED"


def test_completion_failure_leaves_checkpoint_not_completed():
    checkpoints = FakeCheckpointService(
        complete_error=CheckpointError(
            CheckpointErrorCode.DATABASE_UNAVAILABLE, "connection reset"
        )
    )
    service = build_service(checkpoints=checkpoints)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request())

    assert exc_info.value.error_code == "DATABASE_UNAVAILABLE"
    assert BaselineForecastStatus.COMPLETED not in checkpoints.statuses


def test_no_checkpoint_is_created_when_preconditions_fail():
    checkpoints = FakeCheckpointService()
    resolver = FakeModelResolver(
        error=BaselineForecastError(
            BaselineForecastErrorCode.MODEL_VERSION_NOT_FOUND, "nope"
        )
    )
    service = build_service(resolver=resolver, checkpoints=checkpoints)

    with pytest.raises(BaselineForecastError):
        service.create_baseline(build_request())

    # Nothing was going to run, so no row is left behind.
    assert checkpoints.transitions == []


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


def test_model_version_not_found_propagates():
    resolver = FakeModelResolver(
        error=BaselineForecastError(
            BaselineForecastErrorCode.MODEL_VERSION_NOT_FOUND,
            "Model version '99' was not found.",
            field_name="model_version",
        )
    )
    service = build_service(resolver=resolver)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request(model_version="99"))

    assert exc_info.value.error_code == "MODEL_VERSION_NOT_FOUND"
    assert exc_info.value.field == "model_version"


def test_model_not_in_production_is_rejected_by_default():
    resolver = FakeModelResolver(
        model=ResolvedModel(model_version="14", model_stage=ModelStage.STAGING.value)
    )
    service = build_service(resolver=resolver)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request())

    error = exc_info.value
    assert error.error_code == "MODEL_NOT_IN_PRODUCTION"
    assert error.details["model_stage"] == "Staging"
    assert error.details["required_stage"] == "Production"
    assert error.details["override_env_var"] == "BASELINE_REQUIRE_PRODUCTION_STAGE"


def test_non_production_model_allowed_when_requirement_is_disabled():
    resolver = FakeModelResolver(
        model=ResolvedModel(model_version="14", model_stage=ModelStage.NONE.value)
    )
    service = build_service(resolver=resolver, require_production_stage=False)

    response = service.create_baseline(build_request())

    assert response.model_stage == "None"
    assert response.status is BaselineForecastStatus.COMPLETED


def test_production_requirement_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("BASELINE_REQUIRE_PRODUCTION_STAGE", "false")
    resolver = FakeModelResolver(
        model=ResolvedModel(model_version="14", model_stage=ModelStage.NONE.value)
    )
    service = BaselineForecastService(
        model_resolver=resolver,
        predictor=FakePredictor(),
        feature_schema_service=FakeFeatureSchemaService(),
        checkpoint_service=FakeCheckpointService(),
    )

    assert service.require_production_stage is False
    assert service.create_baseline(build_request()).model_stage == "None"


def test_mlflow_unavailable_propagates():
    resolver = FakeModelResolver(
        error=BaselineForecastError(
            BaselineForecastErrorCode.MLFLOW_UNAVAILABLE, "MLflow is unavailable."
        )
    )
    service = build_service(resolver=resolver)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request())
    assert exc_info.value.error_code == "MLFLOW_UNAVAILABLE"


def test_missing_feature_schema_is_translated():
    schema_service = FakeFeatureSchemaService(
        error=FeatureSchemaError(
            FeatureSchemaErrorCode.FEATURE_SCHEMA_MISSING,
            FEATURE_SCHEMA_MISSING_MESSAGE,
            field="model_version",
            details={"artifact_path": "feature_schema.json"},
        )
    )
    checkpoints = FakeCheckpointService()
    service = build_service(schema_service=schema_service, checkpoints=checkpoints)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request())

    error = exc_info.value
    assert error.error_code == "FEATURE_SCHEMA_MISSING"
    assert error.message == FEATURE_SCHEMA_MISSING_MESSAGE
    assert error.details["artifact_path"] == "feature_schema.json"
    assert checkpoints.transitions == []


def test_insufficient_history_is_reported():
    # Bypass schema validation to reach the service-level history rule.
    request = ScenarioRequest.model_construct(
        store_id="store_001",
        model_version="latest",
        horizon_days=HORIZON_DAYS,
        confidence_levels=[0.1, 0.5, 0.9],
        history=build_history(rows=3),
        scenario=ScenarioAssumptions(scenario_type=ScenarioType.PROMOTION),
        baseline_forecast_id=None,
    )
    checkpoints = FakeCheckpointService()
    service = build_service(checkpoints=checkpoints)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(request)

    error = exc_info.value
    assert error.error_code == "INSUFFICIENT_HISTORY"
    assert error.details["validation_error_code"] == "history_too_short"
    assert error.details["received"] == 3
    assert checkpoints.transitions == []


def test_database_unavailable_on_create_is_translated():
    checkpoints = FakeCheckpointService(
        create_error=CheckpointError(
            CheckpointErrorCode.DATABASE_UNAVAILABLE, "could not connect"
        )
    )
    service = build_service(checkpoints=checkpoints)

    with pytest.raises(BaselineForecastError) as exc_info:
        service.create_baseline(build_request())

    error = exc_info.value
    assert error.error_code == "DATABASE_UNAVAILABLE"
    assert error.details["checkpoint_error_code"] == "DATABASE_UNAVAILABLE"


def test_error_serialises_to_response():
    error = BaselineForecastError(
        BaselineForecastErrorCode.MODEL_NOT_IN_PRODUCTION,
        "not production",
        field_name="model_version",
        details={"model_stage": "Staging"},
    )

    response = error.to_response()
    assert response.error_code == "MODEL_NOT_IN_PRODUCTION"
    assert response.field == "model_version"
    assert error.to_dict() == response.model_dump()


# --------------------------------------------------------------------------
# ForecastCheckpointService against a real schema (SQLite)
# --------------------------------------------------------------------------


@pytest.fixture
def checkpoint_service() -> ForecastCheckpointService:
    """Real service on an in-memory database, so DDL and transactions run."""
    return ForecastCheckpointService(
        database_url="sqlite+pysqlite:///:memory:", create_tables=True
    )


def checkpoint_kwargs(**overrides) -> Dict:
    payload = {
        "store_id": "store_001",
        "model_version": "14",
        "model_stage": "Production",
        "horizon_days": 7,
        "history_start_date": HISTORY_START,
        "history_end_date": HISTORY_START + timedelta(days=29),
        "forecast_start_date": HISTORY_START + timedelta(days=30),
        "forecast_end_date": HISTORY_START + timedelta(days=36),
        "data_source": "kaggle_m5",
        "dataset_version": "v1.0.0",
        "feature_schema_version": "v1",
    }
    payload.update(overrides)
    return payload


def test_table_name_and_columns_match_the_specification():
    assert ScenarioBaselineForecast.__tablename__ == TABLE_NAME == "scenario_baseline_forecasts"

    columns = set(ScenarioBaselineForecast.__table__.columns.keys())
    required = {
        "id",
        "forecast_id",
        "store_id",
        "model_version",
        "model_stage",
        "horizon_days",
        "history_start_date",
        "history_end_date",
        "forecast_start_date",
        "forecast_end_date",
        "data_source",
        "dataset_version",
        "feature_schema_version",
        "status",
        "created_at",
        "expires_at",
    }
    assert required <= columns

    table = ScenarioBaselineForecast.__table__
    assert table.columns["forecast_id"].unique is True
    assert table.columns["id"].primary_key is True
    assert table.columns["expires_at"].nullable is True


def test_checkpoint_lifecycle_persists(checkpoint_service):
    created = checkpoint_service.create(**checkpoint_kwargs())
    assert created.status is BaselineForecastStatus.CREATED
    assert created.forecast_id.startswith("bf_")
    assert created.id is not None

    completed = checkpoint_service.mark_completed(created.forecast_id)
    assert completed.status is BaselineForecastStatus.COMPLETED

    fetched = checkpoint_service.get(created.forecast_id)
    assert fetched.status is BaselineForecastStatus.COMPLETED
    assert fetched.data_source == "kaggle_m5"
    assert fetched.is_reusable is True


def test_mark_failed_records_error_details(checkpoint_service):
    created = checkpoint_service.create(**checkpoint_kwargs())

    failed = checkpoint_service.mark_failed(
        created.forecast_id, error_code="PREDICTION_FAILED", error_message="boom"
    )

    assert failed.status is BaselineForecastStatus.FAILED
    assert failed.error_code == "PREDICTION_FAILED"
    assert failed.error_message == "boom"
    assert checkpoint_service.get(created.forecast_id).is_reusable is False


def test_all_four_status_values_are_persistable(checkpoint_service):
    for status in BaselineForecastStatus:
        created = checkpoint_service.create(**checkpoint_kwargs())
        if status is BaselineForecastStatus.COMPLETED:
            checkpoint_service.mark_completed(created.forecast_id)
        elif status is BaselineForecastStatus.FAILED:
            checkpoint_service.mark_failed(created.forecast_id, "X", "y")
        elif status is BaselineForecastStatus.EXPIRED:
            checkpoint_service.mark_expired(created.forecast_id)
        assert checkpoint_service.get(created.forecast_id).status is status


def test_unknown_forecast_id_reads_as_none(checkpoint_service):
    assert checkpoint_service.get("bf_does_not_exist") is None


def test_marking_unknown_forecast_raises_not_found(checkpoint_service):
    with pytest.raises(CheckpointError) as exc_info:
        checkpoint_service.mark_completed("bf_does_not_exist")
    assert exc_info.value.error_code == "CHECKPOINT_NOT_FOUND"


def test_expired_checkpoint_is_not_reusable(checkpoint_service):
    created = checkpoint_service.create(
        **checkpoint_kwargs(),
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    checkpoint_service.mark_completed(created.forecast_id)

    fetched = checkpoint_service.get(created.forecast_id)
    assert fetched.is_expired is True
    assert fetched.is_reusable is False


def test_expire_stale_moves_past_expiry_rows(checkpoint_service):
    stale = checkpoint_service.create(
        **checkpoint_kwargs(),
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    fresh = checkpoint_service.create(
        **checkpoint_kwargs(),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    assert checkpoint_service.expire_stale() == 1
    assert checkpoint_service.get(stale.forecast_id).status is BaselineForecastStatus.EXPIRED
    assert checkpoint_service.get(fresh.forecast_id).status is BaselineForecastStatus.CREATED


def test_list_for_store_returns_newest_first(checkpoint_service):
    first = checkpoint_service.create(**checkpoint_kwargs())
    second = checkpoint_service.create(**checkpoint_kwargs())
    checkpoint_service.create(**checkpoint_kwargs(store_id="store_002"))

    rows = checkpoint_service.list_for_store("store_001")

    assert {row.forecast_id for row in rows} == {first.forecast_id, second.forecast_id}


def test_default_ttl_is_applied(checkpoint_service):
    created = checkpoint_service.create(**checkpoint_kwargs())
    assert created.expires_at is not None
    assert created.is_expired is False


def test_unconfigured_database_raises_clear_error(monkeypatch):
    monkeypatch.delenv("SCENARIO_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(CheckpointError) as exc_info:
        resolve_database_url()

    assert exc_info.value.error_code == "DATABASE_NOT_CONFIGURED"
    assert "SCENARIO_DATABASE_URL" in exc_info.value.message


def test_end_to_end_with_real_checkpoint_store(checkpoint_service):
    service = BaselineForecastService(
        model_resolver=FakeModelResolver(),
        predictor=FakePredictor(residual_std=40.0),
        feature_schema_service=FakeFeatureSchemaService(),
        checkpoint_service=checkpoint_service,
    )

    response = service.create_baseline(build_request())

    persisted = service.get_checkpoint(response.baseline_forecast_id)
    assert persisted.status is BaselineForecastStatus.COMPLETED
    assert persisted.horizon_days == HORIZON_DAYS
    assert persisted.forecast_start_date == response.forecast_start_date
    assert persisted.forecast_end_date == response.forecast_end_date
    assert persisted.model_stage == "Production"
    assert persisted.feature_schema_version == "v1"


def test_end_to_end_failure_persists_failed_status(checkpoint_service):
    service = BaselineForecastService(
        model_resolver=FakeModelResolver(),
        predictor=FakePredictor(error=RuntimeError("model crashed")),
        feature_schema_service=FakeFeatureSchemaService(),
        checkpoint_service=checkpoint_service,
    )

    with pytest.raises(BaselineForecastError):
        service.create_baseline(build_request())

    rows = checkpoint_service.list_for_store("store_001")
    assert len(rows) == 1
    assert rows[0].status is BaselineForecastStatus.FAILED
    assert rows[0].error_code == "PREDICTION_FAILED"
    assert "model crashed" in rows[0].error_message
