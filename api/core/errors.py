"""
Central error taxonomy for the Scenario Planner.

One code set, one HTTP status mapping, one response envelope. The individual
services each raise their own error type (``ScenarioValidationError``,
``FeatureSchemaError``, ``CheckpointError``, ...) and all of them share the same
duck-typed shape: ``error_code`` / ``message`` / ``field`` / ``details``.
``normalize_error`` folds any of those into this taxonomy, so the services stay
unchanged and the HTTP layer has exactly one thing to render.

Everything that reaches a client passes through ``sanitize_text`` /
``sanitize_details``: no stack traces, filesystem paths, connection strings,
object-store keys, or internal hostnames ever leave this module.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional

REQUEST_ID_PREFIX = "req_"
REDACTED = "[redacted]"

GENERIC_INTERNAL_MESSAGE = (
    "The scenario request could not be completed due to an internal error."
)


class ScenarioErrorCode(str, Enum):
    """Every error a Scenario Planner caller can receive."""

    SCENARIO_VALIDATION_ERROR = "SCENARIO_VALIDATION_ERROR"
    SCENARIO_NOT_SUPPORTED = "SCENARIO_NOT_SUPPORTED"
    FEATURE_SCHEMA_MISSING = "FEATURE_SCHEMA_MISSING"
    FEATURE_NOT_AVAILABLE = "FEATURE_NOT_AVAILABLE"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    MODEL_NOT_IN_PRODUCTION = "MODEL_NOT_IN_PRODUCTION"
    MLFLOW_UNAVAILABLE = "MLFLOW_UNAVAILABLE"
    ARTIFACT_DOWNLOAD_FAILED = "ARTIFACT_DOWNLOAD_FAILED"
    PREDICTION_FAILED = "PREDICTION_FAILED"
    FORECAST_ALIGNMENT_ERROR = "FORECAST_ALIGNMENT_ERROR"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    BASELINE_NOT_FOUND = "BASELINE_NOT_FOUND"
    BASELINE_EXPIRED = "BASELINE_EXPIRED"
    SCENARIO_ALREADY_RUNNING = "SCENARIO_ALREADY_RUNNING"
    INTERNAL_SCENARIO_ERROR = "INTERNAL_SCENARIO_ERROR"
    # Required by the idempotency contract.
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    # Additive: a stored scenario has its own identity, so reporting a missing
    # or expired comparison as BASELINE_* would misname the resource.
    SCENARIO_NOT_FOUND = "SCENARIO_NOT_FOUND"
    SCENARIO_EXPIRED = "SCENARIO_EXPIRED"
    # Additive, for the forecast endpoints: a caller who sent too little
    # history, or history whose columns the model cannot consume, needs to be
    # told which of the two it was -- both would otherwise collapse into the
    # generic validation code and the UI could not offer specific guidance.
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    FEATURE_MISMATCH = "FEATURE_MISMATCH"
    # Authentication and authorization. Kept distinct from the validation
    # codes: "who are you" (401), "you may not" (403), and "that field is
    # wrong" (422) call for three different things from the client.
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    REGISTRATION_DISABLED = "REGISTRATION_DISABLED"
    ROLE_NOT_SELF_ASSIGNABLE = "ROLE_NOT_SELF_ASSIGNABLE"
    EMAIL_ALREADY_REGISTERED = "EMAIL_ALREADY_REGISTERED"
    WEAK_PASSWORD = "WEAK_PASSWORD"
    RATE_LIMITED = "RATE_LIMITED"
    # Resources introduced by the final feature set.
    USER_NOT_FOUND = "USER_NOT_FOUND"
    ALERT_NOT_FOUND = "ALERT_NOT_FOUND"
    MONITORING_DATA_UNAVAILABLE = "MONITORING_DATA_UNAVAILABLE"
    REGISTRY_OPERATION_FAILED = "REGISTRY_OPERATION_FAILED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"


ERROR_STATUS_MAP: Dict[ScenarioErrorCode, int] = {
    ScenarioErrorCode.SCENARIO_VALIDATION_ERROR: 422,
    ScenarioErrorCode.SCENARIO_NOT_SUPPORTED: 409,
    ScenarioErrorCode.FEATURE_SCHEMA_MISSING: 409,
    ScenarioErrorCode.FEATURE_NOT_AVAILABLE: 422,
    ScenarioErrorCode.MODEL_NOT_FOUND: 404,
    ScenarioErrorCode.MODEL_NOT_IN_PRODUCTION: 409,
    ScenarioErrorCode.MLFLOW_UNAVAILABLE: 503,
    ScenarioErrorCode.ARTIFACT_DOWNLOAD_FAILED: 502,
    ScenarioErrorCode.PREDICTION_FAILED: 500,
    ScenarioErrorCode.FORECAST_ALIGNMENT_ERROR: 500,
    ScenarioErrorCode.DATABASE_UNAVAILABLE: 503,
    ScenarioErrorCode.BASELINE_NOT_FOUND: 404,
    ScenarioErrorCode.BASELINE_EXPIRED: 409,
    ScenarioErrorCode.SCENARIO_ALREADY_RUNNING: 409,
    ScenarioErrorCode.INTERNAL_SCENARIO_ERROR: 500,
    ScenarioErrorCode.IDEMPOTENCY_KEY_CONFLICT: 409,
    ScenarioErrorCode.SCENARIO_NOT_FOUND: 404,
    ScenarioErrorCode.SCENARIO_EXPIRED: 410,
    ScenarioErrorCode.INSUFFICIENT_HISTORY: 422,
    ScenarioErrorCode.FEATURE_MISMATCH: 409,
    ScenarioErrorCode.NOT_AUTHENTICATED: 401,
    ScenarioErrorCode.INVALID_CREDENTIALS: 401,
    ScenarioErrorCode.TOKEN_EXPIRED: 401,
    ScenarioErrorCode.TOKEN_INVALID: 401,
    ScenarioErrorCode.ACCOUNT_INACTIVE: 403,
    ScenarioErrorCode.ACCOUNT_LOCKED: 423,
    ScenarioErrorCode.PERMISSION_DENIED: 403,
    ScenarioErrorCode.REGISTRATION_DISABLED: 403,
    ScenarioErrorCode.ROLE_NOT_SELF_ASSIGNABLE: 403,
    ScenarioErrorCode.EMAIL_ALREADY_REGISTERED: 409,
    ScenarioErrorCode.WEAK_PASSWORD: 422,
    ScenarioErrorCode.RATE_LIMITED: 429,
    ScenarioErrorCode.USER_NOT_FOUND: 404,
    ScenarioErrorCode.ALERT_NOT_FOUND: 404,
    ScenarioErrorCode.MONITORING_DATA_UNAVAILABLE: 503,
    ScenarioErrorCode.REGISTRY_OPERATION_FAILED: 502,
    ScenarioErrorCode.INVALID_STATE_TRANSITION: 409,
}

# Service-level codes predate this taxonomy. Mapping them here keeps the
# services untouched while giving the HTTP layer a single vocabulary.
LEGACY_CODE_MAP: Dict[str, ScenarioErrorCode] = {
    # feature schema service
    "FEATURE_SCHEMA_MISSING": ScenarioErrorCode.FEATURE_SCHEMA_MISSING,
    "FEATURE_SCHEMA_INVALID": ScenarioErrorCode.ARTIFACT_DOWNLOAD_FAILED,
    "FEATURE_SCHEMA_UNREADABLE": ScenarioErrorCode.ARTIFACT_DOWNLOAD_FAILED,
    "MODEL_VERSION_NOT_FOUND": ScenarioErrorCode.MODEL_NOT_FOUND,
    # baseline forecast service
    "MODEL_NOT_IN_PRODUCTION": ScenarioErrorCode.MODEL_NOT_IN_PRODUCTION,
    "MLFLOW_UNAVAILABLE": ScenarioErrorCode.MLFLOW_UNAVAILABLE,
    "INSUFFICIENT_HISTORY": ScenarioErrorCode.INSUFFICIENT_HISTORY,
    "PREDICTION_FAILED": ScenarioErrorCode.PREDICTION_FAILED,
    "DATABASE_UNAVAILABLE": ScenarioErrorCode.DATABASE_UNAVAILABLE,
    # checkpoint service
    "DATABASE_NOT_CONFIGURED": ScenarioErrorCode.DATABASE_UNAVAILABLE,
    "CHECKPOINT_NOT_FOUND": ScenarioErrorCode.BASELINE_NOT_FOUND,
    # scenario override engine
    "SCENARIO_NOT_SUPPORTED": ScenarioErrorCode.SCENARIO_NOT_SUPPORTED,
    "FEATURE_NOT_AVAILABLE": ScenarioErrorCode.FEATURE_NOT_AVAILABLE,
    "INVALID_DATE_RANGE": ScenarioErrorCode.SCENARIO_VALIDATION_ERROR,
    "HISTORICAL_DATA_MODIFICATION_ATTEMPTED": ScenarioErrorCode.SCENARIO_VALIDATION_ERROR,
    "TARGET_COLUMN_MODIFICATION_ATTEMPTED": ScenarioErrorCode.SCENARIO_VALIDATION_ERROR,
    "PROTECTED_FEATURE_MODIFICATION_ATTEMPTED": ScenarioErrorCode.SCENARIO_VALIDATION_ERROR,
    # impact service
    "FORECAST_ALIGNMENT_ERROR": ScenarioErrorCode.FORECAST_ALIGNMENT_ERROR,
    "MISSING_FORECAST_VALUES": ScenarioErrorCode.PREDICTION_FAILED,
    "EMPTY_FORECAST": ScenarioErrorCode.PREDICTION_FAILED,
    # comparison orchestration
    "BASELINE_NOT_FOUND": ScenarioErrorCode.BASELINE_NOT_FOUND,
    "BASELINE_NOT_REUSABLE": ScenarioErrorCode.BASELINE_EXPIRED,
    "BASELINE_REQUEST_MISMATCH": ScenarioErrorCode.SCENARIO_VALIDATION_ERROR,
    "FEATURE_MATRIX_UNAVAILABLE": ScenarioErrorCode.INTERNAL_SCENARIO_ERROR,
}

# `ScenarioValidationService` emits lowercase snake_case codes. Everything in
# that family is a request-content problem except the support check.
LEGACY_LOWERCASE_OVERRIDES: Dict[str, ScenarioErrorCode] = {
    "unsupported_scenario_type": ScenarioErrorCode.SCENARIO_NOT_SUPPORTED,
}

# Keys whose values are never safe to return, whatever they contain.
SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|credential|access_key|private"
    r"|traceback|stack|dsn|conn(ection)?_?string",
    re.IGNORECASE,
)

# Keys that describe where something lives rather than what went wrong.
LOCATION_KEY_PATTERN = re.compile(
    r"(^|_)(path|dir|directory|uri|url|endpoint|host|hostname|bucket|tracking_uri)(_|$)",
    re.IGNORECASE,
)

_TEXT_REDACTIONS = (
    # scheme://user:pass@host:port/... and bare scheme://host
    re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"]+", re.IGNORECASE),
    # absolute POSIX paths
    re.compile(r"(?<![\w.])/(?:usr|tmp|var|home|opt|etc|private|Users)/[^\s'\",;)]*"),
    # AWS-style access keys
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{8,}\b"),
    # the well-known local MinIO credential
    re.compile(r"\bminioadmin\b", re.IGNORECASE),
)


def new_request_id() -> str:
    """Opaque request identifier."""
    return f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex}"


def utc_timestamp() -> str:
    """Current UTC time as ``2026-10-19T22:46:00Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_text(value: str) -> str:
    """Strip paths, URIs, and credentials from a message."""
    cleaned = value
    for pattern in _TEXT_REDACTIONS:
        cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


def sanitize_details(details: Any, _depth: int = 0) -> Any:
    """
    Recursively redact anything unsafe from an error's details.

    Sensitive and location-describing keys are replaced wholesale; remaining
    strings are passed through ``sanitize_text``.
    """
    if _depth > 6:
        return REDACTED
    if isinstance(details, Mapping):
        cleaned: Dict[str, Any] = {}
        for key, value in details.items():
            name = str(key)
            if SENSITIVE_KEY_PATTERN.search(name) or LOCATION_KEY_PATTERN.search(name):
                cleaned[name] = REDACTED
            else:
                cleaned[name] = sanitize_details(value, _depth + 1)
        return cleaned
    if isinstance(details, (list, tuple)):
        return [sanitize_details(item, _depth + 1) for item in details]
    if isinstance(details, str):
        return sanitize_text(details)
    return details


class ScenarioPlannerError(Exception):
    """
    Base class for every error the Scenario Planner reports to a client.

    Carries its own HTTP status so the handler never needs a lookup table of
    exception classes.
    """

    code: ScenarioErrorCode = ScenarioErrorCode.INTERNAL_SCENARIO_ERROR
    default_message: str = GENERIC_INTERNAL_MESSAGE

    def __init__(
        self,
        message: Optional[str] = None,
        field: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        status_code: Optional[int] = None,
    ) -> None:
        self.message = message or self.default_message
        self.field = field
        self.details: Dict[str, Any] = details or {}
        self.status_code = status_code or ERROR_STATUS_MAP.get(self.code, 500)
        super().__init__(self.message)

    @property
    def error_code(self) -> str:
        """String form of the code, matching the service-level convention."""
        return self.code.value

    @property
    def is_server_error(self) -> bool:
        """Whether this is a 5xx."""
        return self.status_code >= 500

    def to_error_payload(
        self,
        request_id: str,
        timestamp: Optional[str] = None,
        safe: bool = True,
    ) -> Dict[str, Any]:
        """
        Build the client-facing envelope.

        ``safe=False`` is for internal logging only, never for a response.
        """
        message = self.message
        details: Any = self.details

        if safe:
            if self.code is ScenarioErrorCode.INTERNAL_SCENARIO_ERROR:
                # An unexpected failure reveals nothing about its cause.
                message = GENERIC_INTERNAL_MESSAGE
                details = {}
            else:
                message = sanitize_text(message)
                details = sanitize_details(details)

        return {
            "success": False,
            "error": {
                "code": self.code.value,
                "message": message,
                "field": self.field,
                "details": details,
                "request_id": request_id,
                "timestamp": timestamp or utc_timestamp(),
            },
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code.value!r}, "
            f"status={self.status_code}, field={self.field!r})"
        )


class ScenarioValidationError(ScenarioPlannerError):
    """The request was well-formed but violates a business rule. -> 422"""

    code = ScenarioErrorCode.SCENARIO_VALIDATION_ERROR
    default_message = "The scenario request failed validation."


class ScenarioNotSupportedError(ScenarioPlannerError):
    """The selected model cannot express this scenario. -> 409"""

    code = ScenarioErrorCode.SCENARIO_NOT_SUPPORTED
    default_message = "The requested scenario is not supported by this model."


class FeatureSchemaMissingError(ScenarioPlannerError):
    """The model version has no feature schema artifact. -> 409"""

    code = ScenarioErrorCode.FEATURE_SCHEMA_MISSING
    default_message = "Feature schema is not available for this model version."


class FeatureNotAvailableError(ScenarioPlannerError):
    """A requested feature is absent from the model's schema. -> 422"""

    code = ScenarioErrorCode.FEATURE_NOT_AVAILABLE
    default_message = "A requested feature is not available for this model version."


class ModelNotFoundError(ScenarioPlannerError):
    """The requested model version does not exist. -> 404"""

    code = ScenarioErrorCode.MODEL_NOT_FOUND
    default_message = "Requested model version was not found."


class ModelNotInProductionError(ScenarioPlannerError):
    """The model exists but is not in the Production stage. -> 409"""

    code = ScenarioErrorCode.MODEL_NOT_IN_PRODUCTION
    default_message = "Requested model version is not in the Production stage."


class MLflowUnavailableError(ScenarioPlannerError):
    """MLflow could not be reached. -> 503"""

    code = ScenarioErrorCode.MLFLOW_UNAVAILABLE
    default_message = "The model tracking service is currently unavailable."


class ArtifactDownloadFailedError(ScenarioPlannerError):
    """An artifact exists but could not be retrieved or parsed. -> 502"""

    code = ScenarioErrorCode.ARTIFACT_DOWNLOAD_FAILED
    default_message = "A required model artifact could not be retrieved."


class PredictionFailedError(ScenarioPlannerError):
    """The model failed to produce a forecast. -> 500"""

    code = ScenarioErrorCode.PREDICTION_FAILED
    default_message = "The forecast could not be generated."


class InsufficientHistoryError(ScenarioPlannerError):
    """Too few history days to populate the model's lag/rolling inputs. -> 422"""

    code = ScenarioErrorCode.INSUFFICIENT_HISTORY
    default_message = "Not enough history was supplied to generate a forecast."


class FeatureMismatchError(ScenarioPlannerError):
    """The model expects inputs that cannot be built from this request. -> 409"""

    code = ScenarioErrorCode.FEATURE_MISMATCH
    default_message = (
        "The selected model expects features that could not be built from the "
        "supplied history."
    )


class ForecastAlignmentError(ScenarioPlannerError):
    """Baseline and scenario series do not cover identical dates. -> 500"""

    code = ScenarioErrorCode.FORECAST_ALIGNMENT_ERROR
    default_message = "Baseline and scenario forecasts must cover identical dates."


class DatabaseUnavailableError(ScenarioPlannerError):
    """The database could not be reached. -> 503"""

    code = ScenarioErrorCode.DATABASE_UNAVAILABLE
    default_message = "The scenario datastore is currently unavailable."


class BaselineNotFoundError(ScenarioPlannerError):
    """The referenced baseline forecast does not exist. -> 404"""

    code = ScenarioErrorCode.BASELINE_NOT_FOUND
    default_message = "The referenced baseline forecast was not found."


class BaselineExpiredError(ScenarioPlannerError):
    """The referenced baseline is expired or otherwise unusable. -> 409"""

    code = ScenarioErrorCode.BASELINE_EXPIRED
    default_message = "The referenced baseline forecast is no longer usable."


class ScenarioAlreadyRunningError(ScenarioPlannerError):
    """An equivalent scenario is already in flight. -> 409"""

    code = ScenarioErrorCode.SCENARIO_ALREADY_RUNNING
    default_message = "A scenario for this store and model is already running."


class IdempotencyKeyConflictError(ScenarioPlannerError):
    """The idempotency key was reused with a different payload. -> 409"""

    code = ScenarioErrorCode.IDEMPOTENCY_KEY_CONFLICT
    default_message = (
        "This Idempotency-Key was already used with a different request payload."
    )


class ScenarioNotFoundError(ScenarioPlannerError):
    """The referenced scenario comparison does not exist. -> 404"""

    code = ScenarioErrorCode.SCENARIO_NOT_FOUND
    default_message = "The referenced scenario was not found."


class ScenarioExpiredError(ScenarioPlannerError):
    """The stored comparison can no longer be served. -> 410"""

    code = ScenarioErrorCode.SCENARIO_EXPIRED
    default_message = "The stored scenario comparison has expired."


class NotAuthenticatedError(ScenarioPlannerError):
    """No usable credential was presented. -> 401"""

    code = ScenarioErrorCode.NOT_AUTHENTICATED
    default_message = "Authentication is required."


class InvalidCredentialsError(ScenarioPlannerError):
    """
    The email or password was wrong. -> 401

    Deliberately indistinguishable from "no such user": telling a caller which
    half was wrong turns the login form into an account-enumeration oracle.
    """

    code = ScenarioErrorCode.INVALID_CREDENTIALS
    default_message = "Incorrect email or password."


class TokenExpiredError(ScenarioPlannerError):
    """The token was valid but has expired. -> 401"""

    code = ScenarioErrorCode.TOKEN_EXPIRED
    default_message = "Your session has expired. Sign in again."


class TokenInvalidError(ScenarioPlannerError):
    """The token could not be verified. -> 401"""

    code = ScenarioErrorCode.TOKEN_INVALID
    default_message = "The authentication token is not valid."


class AccountInactiveError(ScenarioPlannerError):
    """The account exists but has been deactivated. -> 403"""

    code = ScenarioErrorCode.ACCOUNT_INACTIVE
    default_message = "This account has been deactivated."


class AccountLockedError(ScenarioPlannerError):
    """Too many failed attempts; the account is temporarily locked. -> 423"""

    code = ScenarioErrorCode.ACCOUNT_LOCKED
    default_message = "Too many failed sign-in attempts. Try again shortly."


class PermissionDeniedError(ScenarioPlannerError):
    """The caller is authenticated but lacks the capability. -> 403"""

    code = ScenarioErrorCode.PERMISSION_DENIED
    default_message = "You do not have permission to perform this action."


class RegistrationDisabledError(ScenarioPlannerError):
    """Self-registration is turned off for this deployment. -> 403"""

    code = ScenarioErrorCode.REGISTRATION_DISABLED
    default_message = (
        "Self-registration is disabled. Ask an administrator for an account."
    )


class RoleNotSelfAssignableError(ScenarioPlannerError):
    """The requested role is not one a user may choose at sign-up. -> 403"""

    code = ScenarioErrorCode.ROLE_NOT_SELF_ASSIGNABLE
    default_message = (
        "That role cannot be chosen at sign-up. Ask an administrator for it."
    )


class EmailAlreadyRegisteredError(ScenarioPlannerError):
    """The email is already in use. -> 409"""

    code = ScenarioErrorCode.EMAIL_ALREADY_REGISTERED
    default_message = "That email address is already registered."


class WeakPasswordError(ScenarioPlannerError):
    """The password does not meet the policy. -> 422"""

    code = ScenarioErrorCode.WEAK_PASSWORD
    default_message = "The password does not meet the minimum requirements."


class RateLimitedError(ScenarioPlannerError):
    """Too many requests from this caller. -> 429"""

    code = ScenarioErrorCode.RATE_LIMITED
    default_message = "Too many attempts. Try again shortly."


class UserNotFoundError(ScenarioPlannerError):
    """No such user. -> 404"""

    code = ScenarioErrorCode.USER_NOT_FOUND
    default_message = "That user was not found."


class AlertNotFoundError(ScenarioPlannerError):
    """No such monitoring alert. -> 404"""

    code = ScenarioErrorCode.ALERT_NOT_FOUND
    default_message = "That alert was not found."


class MonitoringDataUnavailableError(ScenarioPlannerError):
    """Monitoring storage could not be read. -> 503"""

    code = ScenarioErrorCode.MONITORING_DATA_UNAVAILABLE
    default_message = "Monitoring data is currently unavailable."


class RegistryOperationFailedError(ScenarioPlannerError):
    """A registry mutation could not be applied upstream. -> 502"""

    code = ScenarioErrorCode.REGISTRY_OPERATION_FAILED
    default_message = "The model registry operation could not be completed."


class InvalidStateTransitionError(ScenarioPlannerError):
    """The requested transition is not legal from the current state. -> 409"""

    code = ScenarioErrorCode.INVALID_STATE_TRANSITION
    default_message = "That change is not valid from the current state."


class InternalScenarioError(ScenarioPlannerError):
    """An unexpected failure. -> 500, with no internal detail disclosed."""

    code = ScenarioErrorCode.INTERNAL_SCENARIO_ERROR
    default_message = GENERIC_INTERNAL_MESSAGE


_CODE_TO_CLASS: Dict[ScenarioErrorCode, type] = {
    ScenarioErrorCode.SCENARIO_VALIDATION_ERROR: ScenarioValidationError,
    ScenarioErrorCode.SCENARIO_NOT_SUPPORTED: ScenarioNotSupportedError,
    ScenarioErrorCode.FEATURE_SCHEMA_MISSING: FeatureSchemaMissingError,
    ScenarioErrorCode.FEATURE_NOT_AVAILABLE: FeatureNotAvailableError,
    ScenarioErrorCode.MODEL_NOT_FOUND: ModelNotFoundError,
    ScenarioErrorCode.MODEL_NOT_IN_PRODUCTION: ModelNotInProductionError,
    ScenarioErrorCode.MLFLOW_UNAVAILABLE: MLflowUnavailableError,
    ScenarioErrorCode.ARTIFACT_DOWNLOAD_FAILED: ArtifactDownloadFailedError,
    ScenarioErrorCode.PREDICTION_FAILED: PredictionFailedError,
    ScenarioErrorCode.FORECAST_ALIGNMENT_ERROR: ForecastAlignmentError,
    ScenarioErrorCode.DATABASE_UNAVAILABLE: DatabaseUnavailableError,
    ScenarioErrorCode.BASELINE_NOT_FOUND: BaselineNotFoundError,
    ScenarioErrorCode.BASELINE_EXPIRED: BaselineExpiredError,
    ScenarioErrorCode.SCENARIO_ALREADY_RUNNING: ScenarioAlreadyRunningError,
    ScenarioErrorCode.IDEMPOTENCY_KEY_CONFLICT: IdempotencyKeyConflictError,
    ScenarioErrorCode.SCENARIO_NOT_FOUND: ScenarioNotFoundError,
    ScenarioErrorCode.SCENARIO_EXPIRED: ScenarioExpiredError,
    ScenarioErrorCode.INTERNAL_SCENARIO_ERROR: InternalScenarioError,
}


def error_class_for(code: ScenarioErrorCode) -> type:
    """Canonical exception class for a taxonomy code."""
    return _CODE_TO_CLASS.get(code, InternalScenarioError)


def map_legacy_code(raw_code: str) -> ScenarioErrorCode:
    """
    Translate a service-level code into the taxonomy.

    Unknown lowercase codes come from the validation service and are treated as
    request-content problems; unknown uppercase codes are treated as internal.
    """
    if not raw_code:
        return ScenarioErrorCode.INTERNAL_SCENARIO_ERROR

    try:
        return ScenarioErrorCode(raw_code)
    except ValueError:
        pass

    if raw_code in LEGACY_CODE_MAP:
        return LEGACY_CODE_MAP[raw_code]
    if raw_code in LEGACY_LOWERCASE_OVERRIDES:
        return LEGACY_LOWERCASE_OVERRIDES[raw_code]
    if raw_code.islower():
        return ScenarioErrorCode.SCENARIO_VALIDATION_ERROR
    return ScenarioErrorCode.INTERNAL_SCENARIO_ERROR


def normalize_error(exc: BaseException) -> ScenarioPlannerError:
    """
    Fold any exception into the taxonomy.

    Already-canonical errors pass through. Service errors are matched on their
    ``error_code``, keeping the original code in ``details.source_error_code``
    so logs and clients can still correlate. Anything else becomes a generic
    internal error that discloses nothing.
    """
    if isinstance(exc, ScenarioPlannerError):
        return exc

    raw_code = getattr(exc, "error_code", None)
    if not isinstance(raw_code, str):
        return InternalScenarioError(details={"exception_type": type(exc).__name__})

    code = map_legacy_code(raw_code)
    message = getattr(exc, "message", None) or str(exc) or None
    details = dict(getattr(exc, "details", None) or {})
    if raw_code != code.value:
        details["source_error_code"] = raw_code

    error = error_class_for(code)(
        message=message,
        field=getattr(exc, "field", None),
        details=details,
    )
    error.__cause__ = exc
    return error
