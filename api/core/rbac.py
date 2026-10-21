"""
Roles, permissions, and the matrix that binds them.

The single source of truth for "who may do what". The API enforces it; the
frontend mirrors it for presentation only. A capability absent from a role's set
is denied, so adding a permission requires an explicit grant rather than
remembering to add a check.

Why a capability enum rather than role checks at each call site
--------------------------------------------------------------
A route that asks "is this user an ADMIN or an ML_ENGINEER?" has to be found and
edited every time the role set changes, and the answer is invisible from the
role definition. A route that asks "may this user PROMOTE_MODEL?" reads as its
own documentation, and the matrix below is the one place a reviewer has to look
to audit access.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Iterable, List, Optional


class Role(str, Enum):
    """A user's role. Exactly one per user."""

    ADMIN = "ADMIN"
    ML_ENGINEER = "ML_ENGINEER"
    ANALYST = "ANALYST"
    BUSINESS_USER = "BUSINESS_USER"
    VIEWER = "VIEWER"

    @classmethod
    def from_value(cls, value: Optional[str]) -> Optional["Role"]:
        """Parse a role name, case-insensitively, or return None."""
        if not value:
            return None
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            return None


class Permission(str, Enum):
    """A single capability a route can require."""

    # Forecasting
    VIEW_FORECAST = "view_forecast"
    GENERATE_FORECAST = "generate_forecast"
    EXPORT_FORECAST = "export_forecast"

    # Scenario planning
    RUN_SCENARIO = "run_scenario"
    VIEW_SCENARIO = "view_scenario"

    # Model performance and registry
    VIEW_MODEL_PERFORMANCE = "view_model_performance"
    VIEW_MODEL_REGISTRY = "view_model_registry"
    VIEW_MODEL_REGISTRY_DETAIL = "view_model_registry_detail"
    PROMOTE_MODEL = "promote_model"
    ARCHIVE_MODEL = "archive_model"
    ROLLBACK_MODEL = "rollback_model"
    TRIGGER_RETRAINING = "trigger_retraining"

    # Monitoring
    VIEW_MONITORING = "view_monitoring"
    VIEW_MONITORING_DETAIL = "view_monitoring_detail"
    MANAGE_ALERTS = "manage_alerts"

    # Administration
    MANAGE_USERS = "manage_users"
    VIEW_AUDIT_LOGS = "view_audit_logs"
    VIEW_ALL_AUDIT_LOGS = "view_all_audit_logs"


#: Everything a read-only account may do. Every other role builds on this, so a
#: new read capability is granted once rather than five times.
_READ_ONLY: FrozenSet[Permission] = frozenset(
    {
        Permission.VIEW_FORECAST,
        Permission.VIEW_SCENARIO,
        Permission.VIEW_MODEL_PERFORMANCE,
        Permission.VIEW_MODEL_REGISTRY,
        Permission.VIEW_MONITORING,
    }
)

#: What a self-service account adds: it can produce things, not change them.
_SELF_SERVICE: FrozenSet[Permission] = _READ_ONLY | {
    Permission.GENERATE_FORECAST,
    Permission.EXPORT_FORECAST,
    Permission.RUN_SCENARIO,
}

#: What an analyst adds: the detailed views, but still no mutations.
_ANALYST: FrozenSet[Permission] = _SELF_SERVICE | {
    Permission.VIEW_MODEL_REGISTRY_DETAIL,
    Permission.VIEW_MONITORING_DETAIL,
}

#: What an ML engineer adds: registry mutations, alerts, and audit visibility.
_ML_ENGINEER: FrozenSet[Permission] = _ANALYST | {
    Permission.PROMOTE_MODEL,
    Permission.ARCHIVE_MODEL,
    Permission.ROLLBACK_MODEL,
    Permission.TRIGGER_RETRAINING,
    Permission.MANAGE_ALERTS,
    Permission.VIEW_AUDIT_LOGS,
}

#: Admin is every permission there is, computed rather than listed: a new
#: permission must not be silently absent from the administrator role.
_ADMIN: FrozenSet[Permission] = frozenset(Permission)

ROLE_PERMISSIONS: Dict[Role, FrozenSet[Permission]] = {
    Role.ADMIN: _ADMIN,
    Role.ML_ENGINEER: _ML_ENGINEER,
    Role.ANALYST: _ANALYST,
    Role.BUSINESS_USER: _SELF_SERVICE,
    Role.VIEWER: _READ_ONLY,
}

#: Roles in order of decreasing privilege, for display and for seeding.
ROLE_ORDER: List[Role] = [
    Role.ADMIN,
    Role.ML_ENGINEER,
    Role.ANALYST,
    Role.BUSINESS_USER,
    Role.VIEWER,
]

#: Human-readable role descriptions, surfaced in the admin UI and the docs.
ROLE_DESCRIPTIONS: Dict[Role, str] = {
    Role.ADMIN: "Full access, including user management and audit logs.",
    Role.ML_ENGINEER: (
        "Promote, archive, and roll back models; trigger retraining; manage "
        "alerts; view audit logs. Cannot manage users."
    ),
    Role.ANALYST: (
        "Generate forecasts, run scenarios, and read detailed model and "
        "monitoring data. Cannot change any model."
    ),
    Role.BUSINESS_USER: (
        "Generate forecasts, run scenarios, and read high-level model health."
    ),
    Role.VIEWER: "Read-only access to forecasts, performance, and monitoring.",
}


def permissions_for(role: Optional[Role]) -> FrozenSet[Permission]:
    """Every permission a role holds; empty for an unknown role."""
    if role is None:
        return frozenset()
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: Optional[Role], permission: Permission) -> bool:
    """Whether a role holds one permission."""
    return permission in permissions_for(role)


def has_all_permissions(role: Optional[Role], permissions: Iterable[Permission]) -> bool:
    """Whether a role holds every permission in a set."""
    granted = permissions_for(role)
    return all(permission in granted for permission in permissions)


def permission_names(role: Optional[Role]) -> List[str]:
    """
    Sorted permission names for a role.

    Sent to the frontend so its presentation layer derives from the same matrix
    the API enforces, instead of hardcoding a second copy that can drift.
    """
    return sorted(permission.value for permission in permissions_for(role))


def matrix_rows() -> List[Dict[str, object]]:
    """
    The permission matrix as rows, for documentation and the settings page.

    One row per capability with a boolean per role, so the rendered table and
    the enforced rules cannot disagree.
    """
    return [
        {
            "capability": permission.value,
            **{role.value: has_permission(role, permission) for role in ROLE_ORDER},
        }
        for permission in Permission
    ]


__all__ = [
    "ROLE_DESCRIPTIONS",
    "ROLE_ORDER",
    "ROLE_PERMISSIONS",
    "Permission",
    "Role",
    "has_all_permissions",
    "has_permission",
    "matrix_rows",
    "permission_names",
    "permissions_for",
]
