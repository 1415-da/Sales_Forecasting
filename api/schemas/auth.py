"""
Request and response contracts for authentication and the current user.

The password policy is enforced in ``api.core.security`` rather than here, so
there is one implementation shared by registration, admin creation, and
password change. The schema only checks the shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from api.core.rbac import ROLE_DESCRIPTIONS, Role, permission_names


class LoginRequest(BaseModel):
    """Credentials presented to ``POST /auth/login``."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=256)

    @field_validator("password")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """A whitespace-only password is a missing password."""
        if not value.strip():
            raise ValueError("a password is required")
        return value


class RegisterRequest(BaseModel):
    """
    Self-registration payload.

    ``role`` is a request, not a grant: the server honours it only when it is
    in the deployment's self-assignable list (``SELF_REGISTRATION_ROLES``,
    which excludes ADMIN by default) and refuses anything else. Omitted, the
    configured default role is used.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=256)
    role: Optional[Role] = None

    @field_validator("full_name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        """Trim incidental whitespace and reject a blank name."""
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("a name is required")
        return trimmed


class RefreshRequest(BaseModel):
    """
    Optional body for ``POST /auth/refresh``.

    The refresh token normally arrives in an HttpOnly cookie. A body field is
    accepted for non-browser clients, which have nowhere to keep a cookie.
    """

    model_config = ConfigDict(extra="forbid")

    refresh_token: Optional[str] = None


class LogoutRequest(BaseModel):
    """Optional body for ``POST /auth/logout``."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: Optional[str] = None
    all_sessions: bool = Field(
        default=False, description="Revoke every session for this account."
    )


class ChangePasswordRequest(BaseModel):
    """Payload for a user changing their own password."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class UserProfile(BaseModel):
    """
    The authenticated user, as the frontend sees them.

    ``permissions`` is sent so the UI derives what to show from the same matrix
    the API enforces, instead of keeping a second hardcoded copy that can
    drift. It is presentation only -- the server re-checks every call.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    email: str
    full_name: str
    role: str
    role_description: str
    is_active: bool
    must_change_password: bool
    permissions: List[str]
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None

    @classmethod
    def from_user(cls, user: object) -> "UserProfile":
        """Project a ``User`` row, resolving its role's permissions."""
        role = Role.from_value(getattr(user, "role", None))
        return cls(
            id=str(getattr(user, "id")),
            email=getattr(user, "email"),
            full_name=getattr(user, "full_name"),
            role=role.value if role else str(getattr(user, "role", "")),
            role_description=ROLE_DESCRIPTIONS.get(role, "") if role else "",
            is_active=bool(getattr(user, "is_active", False)),
            must_change_password=bool(getattr(user, "must_change_password", False)),
            permissions=permission_names(role),
            created_at=getattr(user, "created_at"),
            updated_at=getattr(user, "updated_at"),
            last_login_at=getattr(user, "last_login_at", None),
        )


class TokenResponse(BaseModel):
    """
    A successful authentication.

    The refresh token is **not** in the body by default: it is set as an
    HttpOnly cookie, which JavaScript cannot read and an XSS payload therefore
    cannot exfiltrate. It appears here only when cookie mode is disabled for a
    non-browser client.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds.")
    user: UserProfile
    refresh_token: Optional[str] = None


class LogoutResponse(BaseModel):
    """Result of a logout."""

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    sessions_revoked: int


class AuthConfigResponse(BaseModel):
    """
    What the login page needs before anyone has signed in.

    Lets the frontend hide the registration link when self-registration is off,
    rather than offering a form that always fails.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool = True
    self_registration_enabled: bool
    password_min_length: int
    roles: List[str]
    #: Roles the sign-up form may offer, least privileged first, with the role
    #: a new account gets when none is chosen.
    self_registration_roles: List[str] = Field(default_factory=list)
    default_self_registration_role: Optional[str] = None


class SessionSummary(BaseModel):
    """One live session, for the settings page."""

    model_config = ConfigDict(extra="forbid")

    id: str
    created_at: datetime
    expires_at: datetime
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    current: bool = False


__all__ = [
    "AuthConfigResponse",
    "ChangePasswordRequest",
    "LoginRequest",
    "LogoutRequest",
    "LogoutResponse",
    "RefreshRequest",
    "RegisterRequest",
    "SessionSummary",
    "TokenResponse",
    "UserProfile",
]
