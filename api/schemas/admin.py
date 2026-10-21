"""
Admin contracts: user management, audit log reads, and the permission matrix.

A password is never returned except once, immediately after an admin creates an
account or resets one, and it is never stored in readable form. Everything else
about a user is freely readable by an administrator.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from api.core.rbac import ROLE_DESCRIPTIONS, ROLE_ORDER, Role, matrix_rows


class AdminUserRow(BaseModel):
    """One user, as the admin table shows them."""

    model_config = ConfigDict(extra="forbid")

    id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    must_change_password: bool
    is_locked: bool = False
    failed_login_count: int = 0
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None

    @classmethod
    def from_user(cls, user: Any) -> "AdminUserRow":
        """Project a ``User`` row."""
        return cls(
            id=str(user.id),
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            is_active=bool(user.is_active),
            must_change_password=bool(user.must_change_password),
            is_locked=bool(getattr(user, "is_locked", False)),
            failed_login_count=int(user.failed_login_count or 0),
            created_at=user.created_at,
            updated_at=user.updated_at,
            last_login_at=user.last_login_at,
        )


class AdminUserListResponse(BaseModel):
    """A page of users."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    users: List[AdminUserRow] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class CreateUserRequest(BaseModel):
    """
    Admin-initiated account creation.

    ``password`` is optional: when it is omitted the server generates a strong
    one and returns it once, which avoids an administrator having to invent a
    password and then communicate it.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    role: Role
    password: Optional[str] = Field(default=None, max_length=256)
    is_active: bool = True

    @field_validator("full_name")
    @classmethod
    def _trim(cls, value: str) -> str:
        """Trim and require a non-blank name."""
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("a name is required")
        return trimmed


class UpdateUserRequest(BaseModel):
    """
    Admin-initiated account edit.

    Every field is optional and only the supplied ones are applied, so a
    partial update cannot blank out what it did not mention. There is no
    password field: resetting one is its own audited endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    role: Optional[Role] = None
    is_active: Optional[bool] = None

    @field_validator("full_name")
    @classmethod
    def _trim(cls, value: Optional[str]) -> Optional[str]:
        """Trim and require a non-blank name when one is supplied."""
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("a name cannot be blank")
        return trimmed


class CreatedUserResponse(BaseModel):
    """
    A newly created account.

    ``temporary_password`` is populated only when the server generated one, and
    only in this single response. It is not stored in readable form anywhere.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    user: AdminUserRow
    temporary_password: Optional[str] = None
    message: str = ""


class ResetPasswordResponse(BaseModel):
    """The result of an admin-initiated password reset."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    user: AdminUserRow
    temporary_password: str
    sessions_revoked: int
    message: str = (
        "Share this password over a trusted channel. The user must change it at "
        "next sign-in, and all their existing sessions have been revoked."
    )


class AuditLogRow(BaseModel):
    """One audit record."""

    model_config = ConfigDict(extra="forbid")

    id: str
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    outcome: str
    user_id: Optional[str] = None
    user_email: Optional[str] = None
    user_role: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    ip_address: Optional[str] = None
    request_id: Optional[str] = None
    created_at: datetime

    @classmethod
    def from_row(cls, entry: Any) -> "AuditLogRow":
        """
        Project an ``AuditLog`` row.

        The user agent is deliberately not exposed: it is long, adds nothing to
        the reading of an audit trail, and is stored for forensics rather than
        for display.
        """
        return cls(
            id=str(entry.id),
            action=entry.action,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            outcome=entry.outcome,
            user_id=str(entry.user_id) if entry.user_id else None,
            user_email=entry.user_email,
            user_role=entry.user_role,
            metadata=dict(entry.event_metadata or {}),
            ip_address=entry.ip_address,
            request_id=entry.request_id,
            created_at=entry.created_at,
        )


class AuditLogListResponse(BaseModel):
    """A page of audit records, with the filter options available."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    entries: List[AuditLogRow] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0
    #: Action names the caller may filter by, with their counts. Scoped to the
    #: caller's role, so an ML_ENGINEER is not shown account-management actions
    #: they cannot read.
    available_actions: Dict[str, int] = Field(default_factory=dict)
    #: True when the caller sees every user's activity (ADMIN) rather than the
    #: model- and monitoring-scoped subset (ML_ENGINEER).
    unrestricted: bool = False


class RoleDescription(BaseModel):
    """One role and what it can do."""

    model_config = ConfigDict(extra="forbid")

    role: str
    description: str
    permissions: List[str] = Field(default_factory=list)


class PermissionMatrixResponse(BaseModel):
    """
    The role/permission matrix the API enforces.

    Served rather than duplicated in the frontend so the rendered table and the
    enforced rules cannot disagree.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    roles: List[RoleDescription] = Field(default_factory=list)
    matrix: List[Dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def build(cls) -> "PermissionMatrixResponse":
        """Assemble the matrix from the RBAC module."""
        from api.core.rbac import permission_names

        return cls(
            roles=[
                RoleDescription(
                    role=role.value,
                    description=ROLE_DESCRIPTIONS.get(role, ""),
                    permissions=permission_names(role),
                )
                for role in ROLE_ORDER
            ],
            matrix=matrix_rows(),
        )


__all__ = [
    "AdminUserListResponse",
    "AdminUserRow",
    "AuditLogListResponse",
    "AuditLogRow",
    "CreateUserRequest",
    "CreatedUserResponse",
    "PermissionMatrixResponse",
    "ResetPasswordResponse",
    "RoleDescription",
    "UpdateUserRequest",
]
