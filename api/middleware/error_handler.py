"""
Request-ID middleware and the HTTP error boundary.

Two jobs:

* every request carries a ``request_id`` -- accepted from ``X-Request-ID`` or
  generated -- which is exposed to logs through a ``ContextVar``, attached to
  every error body, and echoed back as a response header;
* every exception leaving the application is rendered as the one documented
  error envelope, with its status drawn from the taxonomy and its content
  passed through redaction.

An unexpected exception is logged in full server-side (with traceback) and
returned as a bare ``INTERNAL_SCENARIO_ERROR`` with no message detail, no
``details``, and no traceback.
"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict, Optional

from api.core.errors import (
    GENERIC_INTERNAL_MESSAGE,
    IdempotencyKeyConflictError,
    ScenarioErrorCode,
    ScenarioPlannerError,
    ScenarioValidationError,
    new_request_id,
    normalize_error,
    utc_timestamp,
)

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
MAX_REQUEST_ID_LENGTH = 128

_request_id_var: ContextVar[Optional[str]] = ContextVar("scenario_request_id", default=None)


def get_request_id() -> Optional[str]:
    """Request id for the current context, if one is set."""
    return _request_id_var.get()


def set_request_id(request_id: str) -> Any:
    """Bind a request id to the current context; returns the reset token."""
    return _request_id_var.set(request_id)


def reset_request_id(token: Any) -> None:
    """Restore the previous request id binding."""
    _request_id_var.reset(token)


def sanitize_request_id(candidate: Optional[str]) -> str:
    """
    Accept a caller-supplied request id, or mint a new one.

    An inbound id is echoed into logs and headers, so it is length-capped and
    restricted to safe characters to prevent header or log injection.
    """
    if not candidate:
        return new_request_id()

    cleaned = "".join(
        char for char in candidate.strip() if char.isalnum() or char in "-_.:"
    )[:MAX_REQUEST_ID_LENGTH]
    return cleaned or new_request_id()


def resolve_status(exc: BaseException) -> int:
    """HTTP status for any exception, via the taxonomy."""
    return normalize_error(exc).status_code


def build_error_payload(
    exc: BaseException, request_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Render the client-facing error envelope.

    Pure and framework-free, so the response contract is testable without an
    HTTP stack.
    """
    error = normalize_error(exc)
    return error.to_error_payload(
        request_id=request_id or get_request_id() or new_request_id(),
        timestamp=utc_timestamp(),
        safe=True,
    )


def log_exception(exc: BaseException, request_id: str, path: str = "") -> None:
    """
    Log a failure, with a traceback only for genuinely unexpected errors.

    Expected errors (a missing model, an unsupported scenario) are ordinary
    outcomes and do not deserve a stack trace in the log.
    """
    error = normalize_error(exc)
    unexpected = error.code is ScenarioErrorCode.INTERNAL_SCENARIO_ERROR

    logger.log(
        logging.ERROR if error.is_server_error else logging.WARNING,
        "request_id=%s path=%s code=%s status=%s message=%s",
        request_id,
        path,
        error.error_code,
        error.status_code,
        error.message,
        exc_info=unexpected,
    )


class RequestIdMiddleware:
    """
    Pure-ASGI middleware that binds and echoes a request id.

    Implemented at the ASGI level rather than with ``BaseHTTPMiddleware`` so the
    ``ContextVar`` binding is visible to the endpoint and to the exception
    handlers that run inside the same task.
    """

    def __init__(self, app: Any, header_name: str = REQUEST_ID_HEADER) -> None:
        self.app = app
        self.header_name = header_name
        self._header_key = header_name.lower().encode("latin-1")

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        inbound = self._read_header(scope)
        request_id = sanitize_request_id(inbound)
        scope.setdefault("state", {})
        if isinstance(scope["state"], dict):
            scope["state"]["request_id"] = request_id
        token = set_request_id(request_id)

        async def send_with_header(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                headers = [
                    (key, value)
                    for key, value in headers
                    if key.lower() != self._header_key
                ]
                headers.append((self._header_key, request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            reset_request_id(token)

    def _read_header(self, scope: Dict[str, Any]) -> Optional[str]:
        """Read the inbound request-id header, if present."""
        for key, value in scope.get("headers") or []:
            if key.lower() == self._header_key:
                try:
                    return value.decode("latin-1")
                except Exception:  # noqa: BLE001 - a malformed header is just absent
                    return None
        return None


def read_idempotency_key(request: Any) -> Optional[str]:
    """Idempotency key from the request headers, if supplied."""
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = headers.get(IDEMPOTENCY_KEY_HEADER)
    return value.strip() if value else None


def install_error_handlers(app: Any) -> Any:
    """
    Register the exception handlers and the request-id middleware on an app.

    Handlers are registered for the canonical taxonomy base class *and* for each
    service-level error type, so an exception raised deep in a service is
    rendered identically to one raised in a route. ``fastapi`` is imported here
    rather than at module scope so this module stays importable (and its pure
    functions testable) without the web stack installed.
    """
    from fastapi.responses import JSONResponse

    async def handle(request: Any, exc: BaseException) -> Any:
        request_id = (
            getattr(getattr(request, "state", None), "request_id", None)
            or get_request_id()
            or new_request_id()
        )
        log_exception(exc, request_id, path=str(getattr(request, "url", "")))
        error = normalize_error(exc)
        return JSONResponse(
            status_code=error.status_code,
            content=error.to_error_payload(
                request_id=request_id, timestamp=utc_timestamp(), safe=True
            ),
            headers={REQUEST_ID_HEADER: request_id},
        )

    async def handle_request_validation(request: Any, exc: BaseException) -> Any:
        """
        Render a schema-level 422 in the same envelope as everything else.

        Without this, FastAPI's default handler answers with its own
        ``{"detail": [...]}`` shape, so a malformed body and a rejected business
        rule -- both 422 -- would reach the client as two different contracts
        and the frontend would need two parsers.
        """
        request_id = (
            getattr(getattr(request, "state", None), "request_id", None)
            or get_request_id()
            or new_request_id()
        )
        error = ScenarioValidationError(
            "The request failed validation.",
            field=_first_invalid_field(exc),
            details={"fields": _validation_field_errors(exc)},
        )
        log_exception(error, request_id, path=str(getattr(request, "url", "")))
        return JSONResponse(
            status_code=error.status_code,
            content=error.to_error_payload(
                request_id=request_id, timestamp=utc_timestamp(), safe=True
            ),
            headers={REQUEST_ID_HEADER: request_id},
        )

    app.add_exception_handler(ScenarioPlannerError, handle)

    for error_type in _service_error_types():
        app.add_exception_handler(error_type, handle)

    try:
        from fastapi.exceptions import RequestValidationError

        app.add_exception_handler(RequestValidationError, handle_request_validation)
    except Exception as exc:  # noqa: BLE001 - optional wiring
        logger.debug("Skipping request-validation handler: %s", exc)

    app.add_exception_handler(Exception, handle)
    app.add_middleware(RequestIdMiddleware)
    return app


def _validation_field_errors(exc: BaseException) -> Dict[str, str]:
    """
    Flatten pydantic's validation errors to ``{"field path": "message"}``.

    Only the location and the message are kept. The ``input`` pydantic attaches
    is the caller's own payload echoed back, which bloats the response and can
    contain values the caller did not intend to see repeated.
    """
    raw = getattr(exc, "errors", None)
    if not callable(raw):
        return {}
    flattened: Dict[str, str] = {}
    try:
        for item in raw():
            location = ".".join(
                str(part) for part in item.get("loc", ()) if part != "body"
            )
            flattened[location or "body"] = str(item.get("msg", "invalid value"))
    except Exception as nested:  # noqa: BLE001 - detail is best-effort
        logger.debug("Could not flatten validation errors: %s", nested)
    return flattened


def _first_invalid_field(exc: BaseException) -> Optional[str]:
    """The first offending field path, for the envelope's ``field``."""
    fields = _validation_field_errors(exc)
    return next(iter(fields), None)


def _service_error_types() -> tuple:
    """
    Service-level exception classes that must map through the taxonomy.

    Imported lazily and defensively: a service that is not installed in a given
    deployment must not stop the error boundary from being installed.
    """
    types: list = []

    def _add(module_path: str, attribute: str) -> None:
        try:
            module = __import__(module_path, fromlist=[attribute])
            types.append(getattr(module, attribute))
        except Exception as exc:  # noqa: BLE001 - optional wiring
            logger.debug("Skipping handler for %s.%s: %s", module_path, attribute, exc)

    _add("api.services.scenario_validation", "ScenarioValidationError")
    _add("api.services.feature_schema_service", "FeatureSchemaError")
    _add("api.services.forecast_checkpoint_service", "CheckpointError")
    _add("api.services.baseline_forecast_service", "BaselineForecastError")
    _add("api.services.scenario_feature_service", "ScenarioOverrideError")
    _add("api.services.scenario_impact_service", "ScenarioImpactError")
    _add("api.services.scenario_forecast_service", "ScenarioComparisonError")
    return tuple(types)


__all__ = [
    "IDEMPOTENCY_KEY_HEADER",
    "REQUEST_ID_HEADER",
    "GENERIC_INTERNAL_MESSAGE",
    "IdempotencyKeyConflictError",
    "RequestIdMiddleware",
    "build_error_payload",
    "get_request_id",
    "install_error_handlers",
    "log_exception",
    "read_idempotency_key",
    "resolve_status",
    "sanitize_request_id",
    "set_request_id",
]
