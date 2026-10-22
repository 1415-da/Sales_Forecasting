"""
The audit log.

An append-only record of who did what. Rows are never updated or deleted by
application code: an audit trail that can be edited is not an audit trail.

Writes are best-effort with respect to the action they describe -- a failure to
log must not undo a successful promotion -- but they are never silently
dropped: the service logs an error so the gap is visible.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from sqlalchemy import ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import JSON_TYPE, TIMESTAMP_TYPE, Base, utcnow
from api.db.models.user import USERS_TABLE

AUDIT_LOGS_TABLE = "audit_logs"


class AuditAction(str, Enum):
    """Every action worth recording."""

    # Authentication
    USER_LOGIN = "user.login"
    USER_LOGIN_FAILED = "user.login_failed"
    USER_LOGOUT = "user.logout"
    TOKEN_REFRESHED = "user.token_refreshed"
    USER_REGISTERED = "user.registered"
    PASSWORD_CHANGED = "user.password_changed"

    # Forecasting and scenarios
    FORECAST_GENERATED = "forecast.generated"
    SCENARIO_CREATED = "scenario.created"
    SCENARIO_COMPARED = "scenario.compared"
    SCENARIO_ROLLED_BACK = "scenario.rolled_back"

    # Model registry
    MODEL_PROMOTED = "model.promoted"
    MODEL_ARCHIVED = "model.archived"
    MODEL_ROLLED_BACK = "model.rolled_back"
    RETRAINING_REQUESTED = "model.retraining_requested"
    CANDIDATE_REJECTED = "model.candidate_rejected"
    DEPLOYMENT_BLOCKED = "model.deployment_blocked"

    # Training data
    DATASET_UPLOADED = "dataset.uploaded"
    DATASET_SEALED = "dataset.sealed"
    DATASET_CONFIGURED = "dataset.configured"

    # Monitoring
    ALERT_ACKNOWLEDGED = "alert.acknowledged"
    ALERT_RESOLVED = "alert.resolved"
    MONITORING_EVALUATED = "monitoring.evaluated"

    # Administration
    USER_CREATED = "admin.user_created"
    USER_UPDATED = "admin.user_updated"
    USER_ROLE_CHANGED = "admin.user_role_changed"
    USER_DEACTIVATED = "admin.user_deactivated"
    USER_ACTIVATED = "admin.user_activated"
    USER_PASSWORD_RESET = "admin.user_password_reset"

    # Denials are recorded too: a pattern of 403s is a signal.
    PERMISSION_DENIED = "security.permission_denied"


class ResourceType(str, Enum):
    """What an action was performed on."""

    USER = "user"
    SESSION = "session"
    FORECAST = "forecast"
    SCENARIO = "scenario"
    MODEL_VERSION = "model_version"
    ALERT = "alert"
    TRAINING_DATASET = "training_dataset"
    TRAINING_REQUEST = "training_request"
    SYSTEM = "system"


#: Actions an ML_ENGINEER may read. They get model and monitoring history, not
#: other people's account activity, which is an administrator's concern.
MODEL_SCOPED_ACTIONS = frozenset(
    {
        AuditAction.MODEL_PROMOTED,
        AuditAction.MODEL_ARCHIVED,
        AuditAction.MODEL_ROLLED_BACK,
        AuditAction.RETRAINING_REQUESTED,
        AuditAction.CANDIDATE_REJECTED,
        AuditAction.DEPLOYMENT_BLOCKED,
        AuditAction.DATASET_UPLOADED,
        AuditAction.DATASET_SEALED,
        AuditAction.DATASET_CONFIGURED,
        AuditAction.ALERT_ACKNOWLEDGED,
        AuditAction.ALERT_RESOLVED,
        AuditAction.MONITORING_EVALUATED,
        AuditAction.FORECAST_GENERATED,
        AuditAction.SCENARIO_CREATED,
        AuditAction.SCENARIO_COMPARED,
        AuditAction.SCENARIO_ROLLED_BACK,
    }
)


class AuditLog(Base):
    """One recorded action."""

    __tablename__ = AUDIT_LOGS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: Nullable, and `SET NULL` on delete: a failed login has no authenticated
    #: user, and deleting a user must not erase what they did.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey(f"{USERS_TABLE}.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Denormalised so the record stays readable after the account is gone.
    user_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True, index=True)
    user_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    #: Whether the action succeeded. A denied attempt is as interesting as a
    #: successful one, so both are stored in the same stream.
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="success")

    #: Action-specific context. Sanitized before it is written: no token, no
    #: password, no connection string ever reaches this column.
    event_metadata: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )

    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow, index=True
    )

    __table_args__ = (
        # The three ways the log is read: one user's history, one action type
        # over time, and one resource's history.
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"AuditLog(action={self.action!r}, resource={self.resource_type!r}:"
            f"{self.resource_id!r}, outcome={self.outcome!r})"
        )


__all__ = [
    "AUDIT_LOGS_TABLE",
    "MODEL_SCOPED_ACTIONS",
    "AuditAction",
    "AuditLog",
    "ResourceType",
]
