"""
Model discovery: registry resolution, listing, and the HTTP contract.

The resolution rules carry real consequences -- serving a Staging model as if
it were Production, or reporting "no models" while three are promoted -- so each
rule is pinned individually.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.core.errors import MLflowUnavailableError, ModelNotFoundError, ModelNotInProductionError
from api.deps import get_forecast_service
from api.main import create_app
from api.services.forecast_service import ForecastService
from api.services.model_registry_service import ModelRegistryService

from .conftest import (
    FakeArtifactService,
    FakeModelVersion,
    FakeMlflowClient,
    FakeRun,
    FakeRunData,
    STORES,
)

# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_latest_resolves_to_the_production_version(registry):
    """``latest`` means the newest Production version, not the newest version."""
    resolved = registry.resolve("ensemble", "latest")
    assert resolved.version == "1"
    assert resolved.stage == "Production"
    assert resolved.is_production is True
    assert resolved.registered_model_name == "sales_forecast_models_ensemble"


def test_newest_production_version_wins(settings, fake_client):
    """Versions are ordered numerically, so 10 beats 9."""
    name = settings.registered_model_name("ensemble")
    fake_client.versions[name] = [
        FakeModelVersion(name=name, version="9"),
        FakeModelVersion(name=name, version="10"),
        FakeModelVersion(name=name, version="2", current_stage="Archived"),
    ]
    registry = ModelRegistryService(settings, client_factory=lambda: fake_client)
    assert registry.resolve("ensemble", "latest").version == "10"


def test_registry_does_not_rely_on_search_registered_models(registry, fake_client):
    """
    Enumeration must avoid the call that is broken against the 2.9.2 server.

    ``search_registered_models()`` returns an empty list in that pairing, so a
    listing built on it would report no models at all.
    """
    serving, _ = registry.list_serving_versions()
    assert len(serving) == 3
    assert "search_registered_models" not in fake_client.calls


def test_metrics_are_attributed_to_the_right_model(registry):
    """``xgboost_rmse`` belongs to xgboost, not to the ensemble."""
    assert registry.resolve("xgboost", "latest").metrics["rmse"] == pytest.approx(82.96)
    assert registry.resolve("ensemble", "latest").metrics["rmse"] == pytest.approx(98.08)
    assert registry.resolve("lightgbm", "latest").metrics["rmse"] == pytest.approx(118.56)


def test_lineage_params_are_exposed_under_stable_names(registry):
    """Dotted MLflow params are projected to stable response fields."""
    lineage = registry.resolve("ensemble", "latest").lineage()
    assert lineage["data_source"] == "kaggle_m5"
    assert lineage["dataset_version"] == "v1.0.0"
    assert lineage["dataset_date_max"] == "2016-04-24"


def test_training_date_is_rendered_as_iso(registry):
    """A millisecond timestamp becomes a readable ISO string."""
    training_date = registry.resolve("ensemble", "latest").training_date
    assert training_date is not None and training_date.endswith("Z")


def test_unknown_model_is_not_found(registry):
    """A model this deployment does not serve is a 404 with the options."""
    with pytest.raises(ModelNotFoundError) as caught:
        registry.resolve("prophet", "latest")
    assert caught.value.status_code == 404
    assert "ensemble" in caught.value.details["available_models"]


def test_unknown_version_is_not_found(registry):
    """So is a version number that was never registered."""
    with pytest.raises(ModelNotFoundError) as caught:
        registry.resolve("ensemble", "42")
    assert caught.value.details["available_versions"] == ["1"]


def test_absent_registered_model_is_not_an_outage(settings, fake_client):
    """
    A name MLflow does not know yields no versions, not a 503.

    "Training has not run yet" and "MLflow is down" are different problems and
    must not share a status.
    """
    fake_client.versions.pop(settings.registered_model_name("ensemble"))
    registry = ModelRegistryService(settings, client_factory=lambda: fake_client)
    with pytest.raises(ModelNotFoundError):
        registry.resolve("ensemble", "latest")


def test_registry_failure_is_a_503(settings):
    """A client whose calls fail is reported as an upstream outage, not a 404."""
    registry = ModelRegistryService(
        settings,
        client_factory=lambda: _RaisingClient(),
    )
    with pytest.raises(MLflowUnavailableError) as caught:
        registry.resolve("ensemble", "latest")
    assert caught.value.status_code == 503


class _RaisingClient:
    """A client whose every registry call fails with a transport error."""

    def search_model_versions(self, filter_string: str):
        """Always fail as a connection problem."""
        raise RuntimeError("HTTPConnectionPool: connection refused")


def test_non_production_version_is_refused_when_production_is_required(
    settings, fake_client
):
    """Serving a Staging model as Production would misreport what ran."""
    name = settings.registered_model_name("ensemble")
    fake_client.versions[name] = [
        FakeModelVersion(name=name, version="1", current_stage="Staging")
    ]
    registry = ModelRegistryService(settings, client_factory=lambda: fake_client)
    with pytest.raises(ModelNotInProductionError) as caught:
        registry.resolve("ensemble", "latest")
    assert caught.value.status_code == 409


def test_non_production_version_is_allowed_when_configured(settings, fake_client):
    """A deployment may opt into serving unpromoted versions."""
    name = settings.registered_model_name("ensemble")
    fake_client.versions[name] = [
        FakeModelVersion(name=name, version="1", current_stage="Staging")
    ]
    relaxed = replace(settings, require_production_stage=False)
    registry = ModelRegistryService(relaxed, client_factory=lambda: fake_client)
    resolved = registry.resolve("ensemble", "latest")
    assert resolved.stage == "Staging"
    assert resolved.is_production is False


def test_unreadable_run_still_yields_a_usable_version(settings, fake_client):
    """Metrics are nice to have; a missing run must not break resolution."""
    fake_client.runs.clear()
    registry = ModelRegistryService(settings, client_factory=lambda: fake_client)
    resolved = registry.resolve("ensemble", "latest")
    assert resolved.version == "1"
    assert resolved.metrics == {}


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


def _service(settings, client, bundle) -> ForecastService:
    """A forecast service over a given fake registry client and bundle."""
    return ForecastService(
        settings=settings,
        registry=ModelRegistryService(settings, client_factory=lambda: client),
        artifacts=FakeArtifactService(bundle),
    )


def test_list_models_describes_every_configured_model(
    settings, fake_client, bundle_factory
):
    """One entry per configured model, with stores from the encoder."""
    response = _service(settings, fake_client, bundle_factory()).list_models()

    assert [info.model for info in response.models] == [
        "ensemble",
        "xgboost",
        "lightgbm",
    ]
    assert response.production_available is True
    assert response.default_model == "ensemble"
    assert [info.is_default for info in response.models] == [True, False, False]
    assert response.supported_stores == STORES
    assert response.models[0].label == "Ensemble"
    assert response.models[0].feature_count == 51
    assert response.models[0].metrics.rmse == pytest.approx(98.08)


def test_list_models_is_empty_with_a_warning_when_nothing_is_registered(
    settings, bundle_factory
):
    """
    An empty registry is a 200 with an explanation, not an error.

    "No models trained yet" is a state the UI renders; retrying would not help.
    """
    empty = FakeMlflowClient(versions={}, runs={})
    response = _service(settings, empty, bundle_factory()).list_models()

    assert response.models == []
    assert response.production_available is False
    assert response.default_model is None
    assert any("training DAG" in warning for warning in response.warnings)


def test_list_models_discloses_an_unpromoted_fallback(settings, fake_client, bundle_factory):
    """Showing a Staging version is fine; passing it off as Production is not."""
    name = settings.registered_model_name("ensemble")
    fake_client.versions[name] = [
        FakeModelVersion(name=name, version="3", current_stage="Staging")
    ]
    response = _service(settings, fake_client, bundle_factory()).list_models()

    ensemble = next(info for info in response.models if info.model == "ensemble")
    assert ensemble.is_production is False
    assert ensemble.stage == "Staging"
    assert any("no Production version" in warning for warning in response.warnings)


def test_list_models_survives_unreadable_artifacts(settings, fake_client, bundle_factory):
    """
    A model whose artifacts will not load is still listed.

    Its store and feature detail is empty and the reason is reported, because a
    model selector is useful even when one entry cannot be introspected.
    """
    artifacts = FakeArtifactService(bundle_factory())
    artifacts.fail_with = RuntimeError("artifact store unreachable")
    service = ForecastService(
        settings=settings,
        registry=ModelRegistryService(settings, client_factory=lambda: fake_client),
        artifacts=artifacts,
    )
    response = service.list_models()

    assert len(response.models) == 3
    assert response.models[0].supported_stores == []
    assert any("could not be read" in warning for warning in response.warnings)


def test_get_model_returns_the_feature_list_and_lineage(
    settings, fake_client, bundle_factory
):
    """Detail includes the ordered feature contract the model consumes."""
    detail = _service(settings, fake_client, bundle_factory()).get_model("latest")

    assert detail.model.version == "1"
    assert len(detail.features) == 51
    assert detail.features[0] == "store_id"
    assert detail.lineage["data_source"] == "kaggle_m5"


def test_get_model_accepts_an_explicit_version_and_model(
    settings, fake_client, bundle_factory
):
    """A specific version of a specific model is addressable."""
    detail = _service(settings, fake_client, bundle_factory()).get_model("1", "xgboost")
    assert detail.model.model == "xgboost"
    assert detail.model.metrics.rmse == pytest.approx(82.96)


# --------------------------------------------------------------------------
# HTTP contract
# --------------------------------------------------------------------------


@pytest.fixture
def client(settings, fake_client, bundle_factory):
    """An app whose forecast service is backed by the fakes."""
    service = _service(settings, fake_client, bundle_factory())
    app = create_app()
    app.dependency_overrides[get_forecast_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client


def test_get_models_returns_the_documented_body(client):
    """The listing shape the model selector consumes."""
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()

    assert body["success"] is True
    assert body["production_available"] is True
    assert body["default_model"] == "ensemble"
    assert {info["model"] for info in body["models"]} == {
        "ensemble",
        "xgboost",
        "lightgbm",
    }
    first = body["models"][0]
    assert set(first) >= {
        "model",
        "label",
        "version",
        "stage",
        "run_id",
        "is_production",
        "metrics",
        "training_date",
        "supported_stores",
    }


def test_get_model_detail_by_version(client):
    """``GET /v1/models/latest`` resolves the Production version."""
    response = client.get("/v1/models/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["model"]["version"] == "1"
    assert len(body["features"]) == 51


def test_get_model_detail_for_a_named_model(client):
    """The short model name is a query parameter on the version path."""
    response = client.get("/v1/models/1", params={"model": "lightgbm"})
    assert response.status_code == 200
    assert response.json()["model"]["model"] == "lightgbm"


def test_get_unknown_model_detail_is_404(client):
    """An unregistered version is a 404 in the shared error envelope."""
    response = client.get("/v1/models/999")
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "MODEL_NOT_FOUND"
    assert "request_id" in body["error"]
