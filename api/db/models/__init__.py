"""
Every ORM model, imported in one place.

Importing this module is what populates ``Base.metadata``. Alembic's
``env.py``, ``create_all``, and the test fixtures all depend on that: a model
defined but never imported is a table that silently does not exist.
"""

from api.db.base import JSON_TYPE, TIMESTAMP_TYPE, Base, utcnow
from api.db.models.audit import (
    MODEL_SCOPED_ACTIONS,
    AuditAction,
    AuditLog,
    ResourceType,
)
from api.db.models.monitoring import (
    Actual,
    Alert,
    AlertSeverity,
    AlertStatus,
    AlertType,
    DriftMetric,
    DriftStatus,
    ForecastPoint,
    ForecastRequestRecord,
    HealthStatus,
    MonitoringMetric,
)
from api.db.models.scenario import (
    ScenarioAuditEvent,
    ScenarioDailyForecast,
    ScenarioForecast,
    ScenarioForecastStatus,
)
from api.db.models.training import (
    DatasetMode,
    DatasetStatus,
    DecisionKind,
    DeploymentDecision,
    DeploymentState,
    TrainingDataset,
    TrainingRequest,
    TrainingStatus,
)
from api.db.models.user import RefreshToken, User

# The scenario checkpoint table is declared in the service module that owns its
# behaviour; imported here so it joins the same metadata registry.
from api.services.forecast_checkpoint_service import ScenarioBaselineForecast

__all__ = [
    "JSON_TYPE",
    "MODEL_SCOPED_ACTIONS",
    "TIMESTAMP_TYPE",
    "Actual",
    "Alert",
    "AlertSeverity",
    "AlertStatus",
    "AlertType",
    "AuditAction",
    "AuditLog",
    "Base",
    "DatasetMode",
    "DatasetStatus",
    "DecisionKind",
    "DeploymentDecision",
    "DeploymentState",
    "DriftMetric",
    "DriftStatus",
    "ForecastPoint",
    "ForecastRequestRecord",
    "HealthStatus",
    "MonitoringMetric",
    "RefreshToken",
    "ResourceType",
    "ScenarioAuditEvent",
    "ScenarioBaselineForecast",
    "ScenarioDailyForecast",
    "ScenarioForecast",
    "ScenarioForecastStatus",
    "TrainingDataset",
    "TrainingRequest",
    "TrainingStatus",
    "User",
    "utcnow",
]
