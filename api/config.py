"""
Runtime configuration for the serving layer.

Everything the API needs to find a dependency, resolve a model, or bound a
request is read from the environment exactly once and frozen into a
``Settings`` instance. No module reaches for ``os.getenv`` on its own, so a
deployment has a single place to look and tests have a single place to patch.

Deliberately dependency-free (no pydantic-settings): the only requirement is
that it be explicit and typed, and adding a package for that is not warranted.

Credentials are read here only so they can be handed to boto3/MLflow inside the
process. They are never placed in a response, a log line, or an error detail.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

# --- Model artifact layout -------------------------------------------------
# Training writes both a flavoured MLflow model (``mlflow_model/<name>``) and the
# raw joblib artifacts below. The raw artifacts are what inference loads: they
# avoid the skops/flavour-version coupling of the MLflow 3.x writer while the
# tracking server is still 2.9.2, and they are the same files the Streamlit
# stack has always read, so model behaviour is unchanged.
SCALERS_ARTIFACT = "scalers.pkl"
ENCODERS_ARTIFACT = "encoders.pkl"
FEATURE_COLS_ARTIFACT = "feature_cols.pkl"

#: Feature scaler key as written by training, and the alias inference uses.
TRAINING_SCALER_KEY = "standard"

#: Same-day columns that the training frame derives from the target itself.
#: ``quantity_sold == sales`` and ``profit == 0.3 * sales`` hold exactly in the
#: M5-derived training data, so these cannot be known for a future date. They
#: are reconstructed from the previous day's value at inference time; see
#: ``api.services.feature_service``.
LEAKED_SAME_DAY_FEATURES: Tuple[str, ...] = ("quantity_sold", "profit")

#: Columns that were constant in the training frame (no source files supplied
#: them), so the distribution-faithful inference value is zero.
ZERO_IN_TRAINING_FEATURES: Tuple[str, ...] = ("customer_traffic", "is_holiday")


def _env(name: str, default: str = "") -> str:
    """Environment value, trimmed, falling back to ``default``."""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    """Integer environment value, falling back on anything unparseable."""
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Float environment value, falling back on anything unparseable."""
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Boolean environment value accepting the usual spellings."""
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: str) -> List[str]:
    """Comma-separated environment value as a list of non-empty items."""
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one process."""

    # Identity
    api_title: str = "Sales Forecasting API"
    api_version: str = "1.0.0"

    # MLflow / MinIO
    mlflow_tracking_uri: str = ""
    mlflow_experiment_name: str = "sales_forecasting"
    minio_endpoint_url: str = ""

    # Model registry
    registered_model_prefix: str = "sales_forecast_models_"
    model_names: Tuple[str, ...] = ("ensemble", "xgboost", "lightgbm")
    default_model: str = "ensemble"
    require_production_stage: bool = True

    # Artifact cache. Not /tmp: a container restart should not force a
    # re-download of every artifact, so this is a path a volume can be mounted
    # over.
    model_cache_dir: str = "/app/.cache/models"
    model_cache_size: int = 3

    # Forecast bounds
    min_history_days: int = 30
    recommended_history_days: int = 60
    max_history_days: int = 1095
    max_horizon_days: int = 90
    allowed_confidence_levels: Tuple[float, ...] = (0.8, 0.95)

    # Feature reconstruction policy
    profit_to_sales_ratio: float = 0.3

    # HTTP
    cors_allow_origins: Tuple[str, ...] = ()
    request_timeout_seconds: float = 120.0

    # Repository root, used to put ``include/`` on the import path so the
    # pickled ensemble (``ml_models.ensemble_model``) can be unpickled.
    repo_root: str = ""

    def registered_model_name(self, model: str) -> str:
        """Registry name for a short model name (``ensemble`` -> prefixed)."""
        return f"{self.registered_model_prefix}{model}"

    def model_artifact_path(self, model: str) -> str:
        """Run-relative path of a model's joblib artifact."""
        return f"models/{model}/{model}_model.pkl"

    @property
    def include_dir(self) -> str:
        """Directory holding the training packages (``ml_models``, ``utils``)."""
        return os.path.join(self.repo_root, "include")


def _default_repo_root() -> str:
    """Repository root inferred from this file's location (``<root>/api``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_settings() -> Settings:
    """Build a ``Settings`` from the current environment."""
    return Settings(
        api_title=_env("API_TITLE", "Sales Forecasting API"),
        api_version=_env("API_VERSION", "1.0.0"),
        mlflow_tracking_uri=_env("MLFLOW_TRACKING_URI"),
        mlflow_experiment_name=_env("MLFLOW_EXPERIMENT_NAME", "sales_forecasting"),
        minio_endpoint_url=_env("MLFLOW_S3_ENDPOINT_URL"),
        registered_model_prefix=_env(
            "MLFLOW_REGISTERED_MODEL_PREFIX", "sales_forecast_models_"
        ),
        model_names=tuple(_env_list("FORECAST_MODELS", "ensemble,xgboost,lightgbm")),
        default_model=_env("FORECAST_DEFAULT_MODEL", "ensemble"),
        require_production_stage=_env_bool("FORECAST_REQUIRE_PRODUCTION_STAGE", True),
        model_cache_dir=_env("MODEL_CACHE_DIR", "/app/.cache/models"),
        model_cache_size=_env_int("MODEL_CACHE_SIZE", 3),
        min_history_days=_env_int("FORECAST_MIN_HISTORY_DAYS", 30),
        recommended_history_days=_env_int("FORECAST_RECOMMENDED_HISTORY_DAYS", 60),
        max_history_days=_env_int("FORECAST_MAX_HISTORY_DAYS", 1095),
        max_horizon_days=_env_int("FORECAST_MAX_HORIZON_DAYS", 90),
        profit_to_sales_ratio=_env_float("FORECAST_PROFIT_TO_SALES_RATIO", 0.3),
        cors_allow_origins=tuple(
            _env_list(
                "API_CORS_ALLOW_ORIGINS",
                "http://localhost:3000,http://127.0.0.1:3000",
            )
        ),
        repo_root=_env("REPO_ROOT", _default_repo_root()),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Process-wide settings.

    Cached because configuration is immutable for a process's lifetime. Tests
    that need different values call ``get_settings.cache_clear()`` after
    patching the environment.
    """
    return load_settings()


def model_label(model: str) -> str:
    """Human-readable name for a short model name."""
    return {
        "ensemble": "Ensemble",
        "xgboost": "XGBoost",
        "lightgbm": "LightGBM",
    }.get(model, model.title())


__all__ = [
    "ENCODERS_ARTIFACT",
    "FEATURE_COLS_ARTIFACT",
    "LEAKED_SAME_DAY_FEATURES",
    "SCALERS_ARTIFACT",
    "TRAINING_SCALER_KEY",
    "ZERO_IN_TRAINING_FEATURES",
    "Settings",
    "get_settings",
    "load_settings",
    "model_label",
]
