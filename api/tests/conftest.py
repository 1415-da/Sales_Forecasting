"""
Shared fixtures for the serving-layer tests.

The point of these doubles is that a forecast can be exercised end to end --
registry lookup, artifact load, feature construction, recursion, uncertainty --
without MLflow, MinIO, a database, or a trained model. Each fake implements
exactly the surface the real collaborator exposes, so a change in that surface
breaks the tests rather than passing silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

import pytest

from api.config import Settings
from api.schemas.forecast import ForecastRequest, HistoryObservation
from api.services.artifact_service import ModelBundle
from api.services.forecast_service import ForecastService
from api.services.model_registry_service import ModelRegistryService

#: The exact 51 features the current Production models consume, in order.
#: Copied from the run's ``feature_cols.pkl`` so a change to the inference
#: feature contract fails here rather than in production.
TRAINED_FEATURE_COLS: List[str] = (
    ["store_id", "quantity_sold", "profit", "has_promotion", "customer_traffic", "is_holiday"]
    + ["year", "month", "day", "dayofweek", "quarter", "weekofyear", "is_weekend"]
    + [f"sales_lag_{lag}" for lag in (1, 2, 3, 7, 14, 21, 30)]
    + [
        f"sales_rolling_{window}_{statistic}"
        for window in (3, 7, 14, 21, 30)
        for statistic in ("mean", "std", "min", "max", "median")
    ]
    + ["month_sin", "month_cos", "day_sin", "day_cos", "dayofweek_sin", "dayofweek_cos"]
)

STORES = ["CA_1", "CA_2", "TX_1", "WI_3"]


# --------------------------------------------------------------------------
# Fake MLflow registry
# --------------------------------------------------------------------------


@dataclass
class FakeModelVersion:
    """Mirrors the attributes the registry service reads off a ModelVersion."""

    name: str
    version: str
    current_stage: str = "Production"
    run_id: Optional[str] = "run-1"
    creation_timestamp: Optional[int] = 1_760_000_000_000
    status: str = "READY"


@dataclass
class FakeRunData:
    """The ``run.data`` shape: metrics and params."""

    metrics: Dict[str, float] = field(default_factory=dict)
    params: Dict[str, str] = field(default_factory=dict)


@dataclass
class FakeRun:
    """The ``get_run`` shape."""

    data: FakeRunData


class FakeMlflowClient:
    """
    Stands in for ``MlflowClient``.

    Only the three calls the registry service makes are implemented, which is
    the point: if it starts calling something else, these tests fail.
    """

    def __init__(
        self,
        versions: Optional[Dict[str, List[FakeModelVersion]]] = None,
        runs: Optional[Dict[str, FakeRun]] = None,
    ) -> None:
        self.versions = versions if versions is not None else {}
        self.runs = runs or {}
        self.calls: List[str] = []

    def search_model_versions(self, filter_string: str) -> List[FakeModelVersion]:
        """Return versions for ``name='...'``, raising for an unknown name."""
        self.calls.append(f"search_model_versions:{filter_string}")
        name = filter_string.split("'")[1]
        if name not in self.versions:
            raise RuntimeError(f"Registered Model with name={name} not found")
        return list(self.versions[name])

    def get_run(self, run_id: str) -> FakeRun:
        """Return the run, raising for an unknown id."""
        self.calls.append(f"get_run:{run_id}")
        if run_id not in self.runs:
            raise RuntimeError(f"Run {run_id} not found")
        return self.runs[run_id]

    def search_registered_models(self) -> List[Any]:
        """
        Deliberately broken, as it is against the deployed server.

        The production tracking server is 2.9.2 while the client may be 3.x; in
        that combination this call returns an empty list. Any code path that
        relied on it would report "no models" while three Production versions
        exist, so the fake reproduces the failure instead of hiding it.
        """
        self.calls.append("search_registered_models")
        return []


# --------------------------------------------------------------------------
# Fake estimator and artifact service
# --------------------------------------------------------------------------


class EchoLagEstimator:
    """
    Predicts the previous day's sales.

    A persistence model, which makes recursion directly observable: every
    forecast day must equal the last observed value.
    """

    def __init__(self, feature_cols: Sequence[str], multiplier: float = 1.0) -> None:
        self.feature_cols = list(feature_cols)
        self.multiplier = multiplier
        self.seen_frames: List[Any] = []

    def predict(self, frame: Any) -> List[float]:
        """Return ``sales_lag_1 * multiplier`` for each row."""
        self.seen_frames.append(frame)
        column = frame["sales_lag_1"]
        return [float(value) * self.multiplier for value in column]


class ConstantEstimator:
    """Predicts a fixed value, for tests about bands rather than levels."""

    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def predict(self, frame: Any) -> List[float]:
        """Return the same value for every row."""
        return [self.value] * len(frame)


class ExplodingEstimator:
    """Raises, to exercise the prediction-failure path."""

    def predict(self, frame: Any) -> List[float]:
        """Always raise."""
        raise RuntimeError("matrix is singular")


class FakeLabelEncoder:
    """Minimal ``LabelEncoder`` substitute over a fixed class list."""

    def __init__(self, classes: Sequence[str]) -> None:
        self.classes_ = list(classes)

    def transform(self, values: Sequence[str]) -> List[int]:
        """Map each value to its index, raising for an unknown one."""
        return [self.classes_.index(value) for value in values]


class IdentityScaler:
    """A scaler that returns its input, so feature values stay readable."""

    def transform(self, frame: Any) -> Any:
        """Return the frame unchanged."""
        return frame.values if hasattr(frame, "values") else frame


class FakeArtifactService:
    """
    Stands in for ``ArtifactService``, serving a prepared bundle.

    ``load_bundle`` records its calls so a test can assert the bundle was
    looked up for the resolved run, not some other one.
    """

    def __init__(self, bundle: Optional[ModelBundle] = None) -> None:
        self.bundle = bundle
        self.calls: List[tuple] = []
        self.fail_with: Optional[Exception] = None

    def load_bundle(self, run_id: str, model_name: str) -> ModelBundle:
        """Return the prepared bundle, or raise a configured failure."""
        self.calls.append((run_id, model_name))
        if self.fail_with is not None:
            raise self.fail_with
        assert self.bundle is not None, "no bundle configured on the fake"
        return self.bundle

    def try_load_bundle(self, run_id: str, model_name: str) -> Optional[ModelBundle]:
        """Non-raising variant, as the real service provides."""
        try:
            return self.load_bundle(run_id, model_name)
        except Exception:  # noqa: BLE001 - mirrors the real contract
            return None


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Settings that do not depend on the environment."""
    return Settings(
        api_version="test",
        mlflow_tracking_uri="http://mlflow.invalid:5001",
        registered_model_prefix="sales_forecast_models_",
        model_names=("ensemble", "xgboost", "lightgbm"),
        default_model="ensemble",
        require_production_stage=True,
        min_history_days=30,
        recommended_history_days=60,
        max_horizon_days=90,
        profit_to_sales_ratio=0.3,
        cors_allow_origins=("http://localhost:3000",),
    )


@pytest.fixture
def run_metrics() -> Dict[str, float]:
    """Metrics in the shape training logs them: ``<model>_<metric>``."""
    return {
        "ensemble_rmse": 98.08,
        "ensemble_mae": 29.64,
        "ensemble_r2": 0.993,
        "xgboost_rmse": 82.96,
        "xgboost_mae": 31.57,
        "xgboost_r2": 0.995,
        "lightgbm_rmse": 118.56,
        "lightgbm_mae": 32.21,
    }


@pytest.fixture
def fake_client(settings: Settings, run_metrics: Dict[str, float]) -> FakeMlflowClient:
    """A registry holding one Production version of each configured model."""
    versions = {
        settings.registered_model_name(model): [
            FakeModelVersion(name=settings.registered_model_name(model), version="1")
        ]
        for model in settings.model_names
    }
    runs = {
        "run-1": FakeRun(
            FakeRunData(
                metrics=dict(run_metrics),
                params={
                    "data.source": "kaggle_m5",
                    "data.version": "v1.0.0",
                    "data.rows": "58327370",
                    "data.date_min": "2011-01-29",
                    "data.date_max": "2016-04-24",
                },
            )
        )
    }
    return FakeMlflowClient(versions=versions, runs=runs)


@pytest.fixture
def registry(settings: Settings, fake_client: FakeMlflowClient) -> ModelRegistryService:
    """A registry service backed by the fake client."""
    return ModelRegistryService(settings=settings, client_factory=lambda: fake_client)


@pytest.fixture
def bundle_factory():
    """Builds a ``ModelBundle`` with the trained feature contract by default."""

    def build(
        model: Any = None,
        feature_cols: Optional[Sequence[str]] = None,
        with_encoder: bool = True,
        with_scaler: bool = True,
        model_name: str = "ensemble",
        run_id: str = "run-1",
    ) -> ModelBundle:
        columns = list(feature_cols or TRAINED_FEATURE_COLS)
        return ModelBundle(
            run_id=run_id,
            model_name=model_name,
            model=model if model is not None else EchoLagEstimator(columns),
            feature_cols=columns,
            scaler=IdentityScaler() if with_scaler else None,
            encoders={"store_id": FakeLabelEncoder(STORES)} if with_encoder else {},
        )

    return build


@pytest.fixture
def forecast_service(settings, registry, bundle_factory):
    """A forecast service wired to fakes, with its artifact double exposed."""

    def build(bundle: Optional[ModelBundle] = None) -> ForecastService:
        artifacts = FakeArtifactService(bundle or bundle_factory())
        service = ForecastService(
            settings=settings, registry=registry, artifacts=artifacts
        )
        service.test_artifacts = artifacts  # type: ignore[attr-defined]
        return service

    return build


def make_history(
    days: int = 60,
    end: date = date(2026, 10, 20),
    level: float = 3000.0,
    store_id: Optional[str] = None,
    **extra: Any,
) -> List[HistoryObservation]:
    """
    A contiguous, mildly seasonal sales history ending on ``end``.

    Deterministic: a forecast assertion must not depend on a random draw.
    """
    start = end - timedelta(days=days - 1)
    observations: List[HistoryObservation] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        # A small weekday effect, so rolling std is non-zero and the series is
        # not trivially constant.
        sales = level + 100.0 * ((offset % 7) - 3)
        payload: Dict[str, Any] = {"date": day, "sales": sales}
        if store_id:
            payload["store_id"] = store_id
        payload.update(extra)
        observations.append(HistoryObservation(**payload))
    return observations


def make_request(**overrides: Any) -> ForecastRequest:
    """A valid forecast request, with any field overridden."""
    payload: Dict[str, Any] = {
        "store_id": "CA_1",
        "model": "ensemble",
        "model_version": "latest",
        "horizon_days": 7,
        "confidence_level": 0.95,
        "history": make_history(),
    }
    payload.update(overrides)
    return ForecastRequest(**payload)
