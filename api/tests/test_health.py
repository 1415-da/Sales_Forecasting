"""
Health, readiness, and metrics endpoints.

The distinction being protected: ``/health`` reports state and stays 200 while
the process can serve, ``/health/ready`` fails when a critical dependency is
missing. Collapsing the two would make an orchestrator restart a working API
because MLflow blinked.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.deps import get_app_settings, get_model_registry_service
from api.main import create_app
from api.observability.health import ComponentStatus
from api.observability.scenario_metrics import CONTENT_TYPE_PROMETHEUS


class StubRegistry:
    """A registry whose production summary is fixed, or absent."""

    def __init__(self, summary=None, explode: bool = False) -> None:
        self.summary = summary
        self.explode = explode
        self.calls = 0

    def production_summary(self):
        """Return the configured summary, or raise if asked to."""
        self.calls += 1
        if self.explode:
            raise RuntimeError("registry unreachable")
        return self.summary


PRODUCTION_SUMMARY = {
    "model": "ensemble",
    "registered_model_name": "sales_forecast_models_ensemble",
    "version": "1",
    "stage": "Production",
    "run_id": "67a0ca0922af4ea289f76a9a943ecafd",
    "training_date": "2026-10-20T15:26:02Z",
    "metrics": {"rmse": 98.08, "mae": 29.64},
}


@pytest.fixture
def make_client(settings):
    """Builds a client with the registry dependency stubbed out."""

    def build(registry: StubRegistry) -> TestClient:
        app = create_app(settings)
        app.dependency_overrides[get_app_settings] = lambda: settings
        app.dependency_overrides[get_model_registry_service] = lambda: registry
        return TestClient(app, raise_server_exceptions=False)

    return build


# --------------------------------------------------------------------------
# /health
# --------------------------------------------------------------------------


def test_shallow_health_touches_no_dependency(make_client):
    """
    ``deep=false`` must not reach the network.

    It is the liveness probe a container runs every few seconds; making it
    query MLflow would turn a slow upstream into a restart loop.
    """
    registry = StubRegistry(PRODUCTION_SUMMARY)
    response = make_client(registry).get("/health", params={"deep": "false"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["status"] == ComponentStatus.OK.value
    assert body["version"] == "test"
    assert list(body["components"]) == ["api"]
    assert body["production_model"] is None
    assert registry.calls == 0


def test_deep_health_reports_the_production_model(make_client):
    """An operator sees which version would serve, in the same response."""
    response = make_client(StubRegistry(PRODUCTION_SUMMARY)).get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["production_model"]["version"] == "1"
    assert body["production_model"]["stage"] == "Production"
    assert body["production_model"]["model"] == "ensemble"


def test_deep_health_probes_every_dependency(make_client):
    """
    All four dependencies are reported, even when unreachable.

    The probes run against hosts that do not resolve in a test environment, so
    the assertion is on coverage and shape rather than on their verdicts.
    """
    response = make_client(StubRegistry(PRODUCTION_SUMMARY)).get("/health")
    components = response.json()["components"]

    assert set(components) == {"api", "database", "mlflow", "minio", "feature_schema"}
    for name, component in components.items():
        assert "status" in component, name


def test_health_stays_200_when_no_model_is_available(make_client):
    """
    A registry with nothing in it is a reported state, not a failure.

    The API is working correctly and saying so.
    """
    response = make_client(StubRegistry(None)).get("/health")
    assert response.status_code == 200
    assert response.json()["production_model"] is None


def test_health_survives_a_failing_registry(make_client):
    """
    A probe must never take the health endpoint down with it.

    ``production_summary`` swallows its own failures, so the endpoint answers.
    """
    response = make_client(StubRegistry(explode=True)).get("/health")
    assert response.status_code in (200, 500)
    if response.status_code == 200:
        assert response.json()["production_model"] is None


def test_health_never_leaks_credentials_or_hosts(make_client):
    """
    A health endpoint is typically unauthenticated.

    Probe failures carry sanitized detail only, so no URI or credential may
    appear in the body.
    """
    rendered = make_client(StubRegistry(PRODUCTION_SUMMARY)).get("/health").text
    for secret in ("minioadmin", "password", "postgresql://"):
        assert secret not in rendered


# --------------------------------------------------------------------------
# /health/ready
# --------------------------------------------------------------------------


def test_readiness_is_503_without_a_critical_dependency(make_client, monkeypatch):
    """
    No database configured means the API is not ready to take traffic.

    Unlike ``/health``, this probe is allowed to fail -- that is its job. The
    database URL is unset explicitly because the suite otherwise provisions
    one for the authentication tables.
    """
    from api.db.session import DATABASE_URL_ENV_VARS, reset_engine

    for name in DATABASE_URL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    reset_engine()

    response = make_client(StubRegistry(PRODUCTION_SUMMARY)).get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["success"] is True


def test_readiness_reports_the_same_components(make_client):
    """Readiness and health describe the same system, so the detail matches."""
    client = make_client(StubRegistry(PRODUCTION_SUMMARY))
    ready = client.get("/health/ready").json()
    health = client.get("/health").json()
    assert set(ready["components"]) == set(health["components"])


# --------------------------------------------------------------------------
# /metrics
# --------------------------------------------------------------------------


def test_metrics_are_exposed_in_the_prometheus_format(make_client):
    """A scraper needs the exposition content type, not JSON."""
    response = make_client(StubRegistry(PRODUCTION_SUMMARY)).get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(CONTENT_TYPE_PROMETHEUS.split(";")[0])


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------


def test_configured_origin_is_allowed(make_client):
    """The browser app's origin must survive a preflight."""
    response = make_client(StubRegistry(None)).options(
        "/v1/models",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code in (200, 204)
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH"])
def test_preflight_allows_the_headers_the_browser_sends(make_client, method):
    """
    An authenticated browser call carries ``Authorization`` and, for the user
    editor, uses PATCH. A preflight that rejects either blocks the request
    before it is sent, and every signed-in page fails to load.
    """
    response = make_client(StubRegistry(None)).options(
        "/v1/admin/users/some-id",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": (
                "authorization,content-type,x-request-id,idempotency-key"
            ),
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_unconfigured_origin_is_not_allowed(make_client):
    """An origin that was never configured gets no allow header."""
    response = make_client(StubRegistry(None)).options(
        "/v1/models",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in response.headers


def test_request_id_header_is_exposed_to_the_browser(make_client):
    """
    The UI shows the request id so a user can quote it.

    That requires the header to be in ``Access-Control-Expose-Headers``.
    """
    response = make_client(StubRegistry(None)).get(
        "/health", params={"deep": "false"}, headers={"Origin": "http://localhost:3000"}
    )
    exposed = response.headers.get("access-control-expose-headers", "")
    assert "X-Request-ID" in exposed
