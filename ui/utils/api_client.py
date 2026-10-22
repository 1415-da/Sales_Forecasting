"""
HTTP client for the forecast API.

The console's only route to a model. It holds no MLflow or MinIO client and is
given no object-store credentials, which is what makes direct artifact access
impossible here rather than merely discouraged.

Every failure is returned as an ``ApiResult`` rather than raised: a Streamlit
script re-runs top to bottom, and an unhandled exception replaces the whole page
with a traceback instead of showing the operator which dependency is down.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

DEFAULT_TIMEOUT_SECONDS = 20
FORECAST_TIMEOUT_SECONDS = 90


def api_base_url() -> str:
    """Base URL of the forecast API."""
    return os.getenv("FORECAST_API_URL", "http://localhost:8000").rstrip("/")


@dataclass
class ApiResult:
    """Outcome of one API call: either ``data`` or an explained failure."""

    ok: bool
    data: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_field: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    status_code: Optional[int] = None
    request_id: Optional[str] = None

    @property
    def payload(self) -> Dict[str, Any]:
        """The data, or an empty dict, so callers can index without a guard."""
        return self.data or {}

    def summary(self) -> str:
        """One line describing the failure, for display."""
        if self.ok:
            return "ok"
        parts: List[str] = []
        if self.error_code:
            parts.append(self.error_code)
        if self.error_message:
            parts.append(self.error_message)
        if self.error_field:
            parts.append(f"(field: {self.error_field})")
        return " — ".join(parts) or "The request failed."


def _new_request_id() -> str:
    """Request id matching the API's convention, so logs can be correlated."""
    return f"req_{uuid.uuid4().hex}"


def _call(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    token: Optional[str] = None,
) -> ApiResult:
    """Make one request and normalise every outcome into an ``ApiResult``."""
    url = f"{api_base_url()}{path}"
    request_id = _new_request_id()
    headers = {"Accept": "application/json", "X-Request-ID": request_id}
    # Every /v1 route requires a bearer token. It is passed per call rather than
    # held here because one Streamlit process serves many browser sessions.
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.request(
            method, url, json=body, headers=headers, timeout=timeout
        )
    except requests.exceptions.Timeout:
        return ApiResult(
            ok=False,
            error_code="API_TIMEOUT",
            error_message=f"The API did not respond within {timeout}s.",
            request_id=request_id,
        )
    except requests.exceptions.RequestException as exc:
        return ApiResult(
            ok=False,
            error_code="API_UNREACHABLE",
            error_message=(
                f"Could not reach the forecast API at {api_base_url()}. "
                "Check that the api service is running."
            ),
            details={"error": str(exc)},
            request_id=request_id,
        )

    echoed = response.headers.get("X-Request-ID", request_id)

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return ApiResult(
            ok=False,
            error_code="API_BAD_RESPONSE",
            error_message="The API returned a response that is not JSON.",
            status_code=response.status_code,
            request_id=echoed,
        )

    if response.ok:
        return ApiResult(
            ok=True, data=payload, status_code=response.status_code, request_id=echoed
        )

    # The API's documented error envelope.
    envelope = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(envelope, dict):
        return ApiResult(
            ok=False,
            error_code=envelope.get("code"),
            error_message=envelope.get("message"),
            error_field=envelope.get("field"),
            details=envelope.get("details") or {},
            status_code=response.status_code,
            request_id=envelope.get("request_id") or echoed,
        )

    # FastAPI's own validation error does not use the envelope.
    return ApiResult(
        ok=False,
        error_code="REQUEST_INVALID",
        error_message="The request was rejected as malformed.",
        details={"body": payload},
        status_code=response.status_code,
        request_id=echoed,
    )


def get_health() -> ApiResult:
    """API health, dependency state, and the current Production model."""
    return _call("GET", "/health")


def login(email: str, password: str) -> ApiResult:
    """
    Sign in and obtain an access token (``data["access_token"]``).

    The refresh cookie the API also sets is not used: when the access token
    expires the console asks the operator to sign in again.
    """
    return _call("POST", "/auth/login", body={"email": email, "password": password})


def get_models(token: Optional[str] = None) -> ApiResult:
    """The model version serving each configured model."""
    return _call("GET", "/v1/models", token=token)


def get_model(
    version: str, model: Optional[str] = None, token: Optional[str] = None
) -> ApiResult:
    """Detail for one model version, including its feature list."""
    query = f"?model={model}" if model else ""
    return _call("GET", f"/v1/models/{version}{query}", token=token)


def create_forecast(
    store_id: str,
    model: str,
    model_version: str,
    horizon_days: int,
    confidence_level: float,
    history: List[Dict[str, Any]],
    token: Optional[str] = None,
) -> ApiResult:
    """
    Generate a forecast.

    A longer timeout than the metadata reads: the call runs recursive inference
    for up to 90 steps on the server.
    """
    return _call(
        "POST",
        "/v1/forecast",
        body={
            "store_id": store_id,
            "model": model,
            "model_version": model_version,
            "horizon_days": int(horizon_days),
            "confidence_level": float(confidence_level),
            "history": history,
        },
        timeout=FORECAST_TIMEOUT_SECONDS,
        token=token,
    )


__all__ = [
    "ApiResult",
    "api_base_url",
    "create_forecast",
    "get_health",
    "get_model",
    "get_models",
    "login",
]
