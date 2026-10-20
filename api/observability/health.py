"""
Health probes for the serving layer.

Two distinct questions, deliberately separated:

* **Liveness** (``/health``) -- is this process able to serve? It reports every
  dependency's state but stays 200 while the API itself is up, so an orchestrator
  does not restart a healthy process because MLflow is briefly down.
* **Readiness** (``/health/ready``) -- should traffic be routed here? This
  returns 503 when a dependency the Scenario Planner cannot work without is
  unavailable.

Every probe is bounded by a short timeout and reports only a status and a
sanitized reason: a health endpoint is usually unauthenticated, so it must not
leak hostnames, paths, or credentials.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from api.core.errors import sanitize_text

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 2.0


class ComponentStatus(str, Enum):
    """State of one dependency."""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not_configured"


#: Dependencies the planner cannot function without. MLflow and MinIO are
#: needed to *run* a scenario but not to serve capability or history reads, so
#: they degrade rather than fail readiness.
CRITICAL_COMPONENTS = ("database",)


@dataclass
class ComponentHealth:
    """One dependency's probe result."""

    name: str
    status: ComponentStatus
    latency_ms: Optional[int] = None
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialisable form, with a sanitized detail string."""
        payload: Dict[str, Any] = {"status": self.status.value}
        if self.latency_ms is not None:
            payload["latency_ms"] = self.latency_ms
        if self.detail:
            payload["detail"] = sanitize_text(self.detail)
        return payload

    @property
    def is_ok(self) -> bool:
        """Whether this dependency is usable."""
        return self.status is ComponentStatus.OK


@dataclass
class HealthReport:
    """The aggregate health of the serving layer."""

    status: ComponentStatus
    version: str
    components: List[ComponentHealth] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """
        The response body.

        ``success`` describes the *report*, not the verdict: the probe ran and
        answered, so it is always true on a 200. Health itself is carried by
        ``status``, and readiness by the HTTP status of ``/health/ready``.
        Returning ``success: false`` on a 200 would contradict the error
        envelope, where that flag only ever accompanies a 4xx or 5xx.
        """
        return {
            "success": True,
            "status": self.status.value,
            "version": self.version,
            "components": {item.name: item.to_dict() for item in self.components},
        }

    @property
    def is_ready(self) -> bool:
        """Whether every critical dependency is usable."""
        return all(
            item.is_ok for item in self.components if item.name in CRITICAL_COMPONENTS
        )


def _timed(name: str, probe: Callable[[], Optional[str]]) -> ComponentHealth:
    """
    Run one probe, timing it and converting any failure into a status.

    A probe must never raise: an exception here would make the health endpoint
    itself unhealthy.
    """
    started = time.perf_counter()
    try:
        detail = probe()
        latency = int((time.perf_counter() - started) * 1000)
        return ComponentHealth(name, ComponentStatus.OK, latency, detail)
    except _NotConfigured as exc:
        return ComponentHealth(name, ComponentStatus.NOT_CONFIGURED, None, str(exc))
    except Exception as exc:  # noqa: BLE001 - a failed probe is a result, not a crash
        latency = int((time.perf_counter() - started) * 1000)
        logger.info("Health probe %s failed: %s", name, exc)
        return ComponentHealth(name, ComponentStatus.UNAVAILABLE, latency, str(exc))


class _NotConfigured(Exception):
    """A dependency has no configuration, so it is not merely unreachable."""


def _http_probe(url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> Optional[str]:
    """GET a health URL, raising when it does not answer 2xx."""
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        code = response.getcode()
        if code and 200 <= code < 300:
            return None
        raise RuntimeError(f"unexpected status {code}")


def probe_database() -> ComponentHealth:
    """Check the scenario datastore with a trivial query."""

    def run() -> Optional[str]:
        from sqlalchemy import text

        from api.services.forecast_checkpoint_service import (
            DATABASE_URL_ENV_VARS,
            ForecastCheckpointService,
        )

        if not any(os.getenv(name) for name in DATABASE_URL_ENV_VARS):
            raise _NotConfigured("no scenario database URL configured")

        service = ForecastCheckpointService()
        with service.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return None

    return _timed("database", run)


def probe_mlflow() -> ComponentHealth:
    """Check the MLflow tracking server's health endpoint."""

    def run() -> Optional[str]:
        base = os.getenv("MLFLOW_TRACKING_URI")
        if not base:
            raise _NotConfigured("MLFLOW_TRACKING_URI is not set")
        return _http_probe(f"{base.rstrip('/')}/health")

    return _timed("mlflow", run)


def probe_minio() -> ComponentHealth:
    """Check MinIO's liveness endpoint. No credentials are used or logged."""

    def run() -> Optional[str]:
        base = os.getenv("MLFLOW_S3_ENDPOINT_URL")
        if not base:
            raise _NotConfigured("MLFLOW_S3_ENDPOINT_URL is not set")
        return _http_probe(f"{base.rstrip('/')}/minio/health/live")

    return _timed("minio", run)


def probe_feature_schema(model_version: str = "latest") -> ComponentHealth:
    """
    Check that a feature schema can be resolved for the current model.

    Reported as *degraded* rather than unavailable when the artifact is simply
    missing: the API is working correctly and saying so, and training not yet
    publishing the artifact is a known gap rather than an outage.
    """
    from api.services.feature_schema_service import FeatureSchemaError, FeatureSchemaService

    # Without a tracking URI, MLflow would fall back to a local store and
    # *create* it -- a health probe must never have side effects on disk.
    if not os.getenv("MLFLOW_TRACKING_URI"):
        return ComponentHealth(
            "feature_schema",
            ComponentStatus.NOT_CONFIGURED,
            None,
            "MLFLOW_TRACKING_URI is not set",
        )

    started = time.perf_counter()
    try:
        schema = FeatureSchemaService().get_feature_schema(model_version)
        latency = int((time.perf_counter() - started) * 1000)
        return ComponentHealth(
            "feature_schema",
            ComponentStatus.OK,
            latency,
            f"{len(schema.features)} features (schema {schema.schema_version})",
        )
    except FeatureSchemaError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return ComponentHealth(
            "feature_schema", ComponentStatus.DEGRADED, latency, exc.error_code
        )
    except Exception as exc:  # noqa: BLE001 - a failed probe is a result
        latency = int((time.perf_counter() - started) * 1000)
        return ComponentHealth(
            "feature_schema", ComponentStatus.UNAVAILABLE, latency, str(exc)
        )


def build_health_report(version: str, deep: bool = True) -> HealthReport:
    """
    Assemble the health report.

    ``deep=False`` skips the dependency probes, for a cheap liveness check that
    never blocks on the network.
    """
    api_component = ComponentHealth("api", ComponentStatus.OK)
    if not deep:
        return HealthReport(ComponentStatus.OK, version, [api_component])

    components = [
        api_component,
        probe_database(),
        probe_mlflow(),
        probe_minio(),
        probe_feature_schema(),
    ]

    critical_down = any(
        item.name in CRITICAL_COMPONENTS and not item.is_ok for item in components
    )
    any_down = any(not item.is_ok for item in components)

    status = (
        ComponentStatus.UNAVAILABLE
        if critical_down
        else ComponentStatus.DEGRADED
        if any_down
        else ComponentStatus.OK
    )
    return HealthReport(status, version, components)


__all__ = [
    "CRITICAL_COMPONENTS",
    "PROBE_TIMEOUT_SECONDS",
    "ComponentHealth",
    "ComponentStatus",
    "HealthReport",
    "build_health_report",
    "probe_database",
    "probe_feature_schema",
    "probe_minio",
    "probe_mlflow",
]
