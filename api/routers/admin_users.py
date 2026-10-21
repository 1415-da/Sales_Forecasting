"""
User administration. ADMIN only.

``require_permission(Permission.MANAGE_USERS)`` is declared on the router, so
every route inherits it and a new one cannot be added unprotected by
forgetting a decorator.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from api.core.rbac import Permission, Role
from api.db.models.user import User
from api.db.session import get_session
from api.dependencies.auth import actor_for, require_permission
from api.schemas.admin import (
    AdminUserListResponse,
    AdminUserRow,
    CreatedUserResponse,
    CreateUserRequest,
    PermissionMatrixResponse,
    ResetPasswordResponse,
    UpdateUserRequest,
)
from api.services.user_service import UserService

logger = logging.getLogger(__name__)

TAG = "Administration"

#: Declared at router level so every route below is ADMIN-gated by
#: construction rather than by remembering to add a dependency.
router = APIRouter(
    prefix="/v1/admin",
    tags=[TAG],
    dependencies=[Depends(require_permission(Permission.MANAGE_USERS))],
    responses={403: {"description": "Requires user-management permission."}},
)


@router.get("/users", response_model=AdminUserListResponse, summary="List users")
def list_users(
    role: Optional[Role] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
    search: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> AdminUserListResponse:
    """A page of accounts, newest first, filterable by role, status, or text."""
    users, total = UserService(session).list_users(
        role=role, is_active=is_active, search=search, limit=limit, offset=offset
    )
    return AdminUserListResponse(
        users=[AdminUserRow.from_user(user) for user in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/permissions",
    response_model=PermissionMatrixResponse,
    summary="The role and permission matrix",
)
def permission_matrix() -> PermissionMatrixResponse:
    """
    The matrix the API enforces.

    Served rather than duplicated in the frontend so a rendered table and the
    enforced rules cannot drift apart.
    """
    return PermissionMatrixResponse.build()


@router.get(
    "/users/{user_id}",
    response_model=AdminUserRow,
    summary="One user",
    responses={404: {"description": "No such user."}},
)
def get_user(user_id: str, session: Session = Depends(get_session)) -> AdminUserRow:
    """One account by id."""
    return AdminUserRow.from_user(UserService(session).require_user(user_id))


@router.post(
    "/users",
    response_model=CreatedUserResponse,
    status_code=201,
    summary="Create a user",
    responses={
        409: {"description": "That email address is already registered."},
        422: {"description": "The supplied password does not meet the policy."},
    },
)
def create_user(
    payload: CreateUserRequest,
    request: Request,
    admin: User = Depends(require_permission(Permission.MANAGE_USERS)),
    session: Session = Depends(get_session),
) -> CreatedUserResponse:
    """
    Create an account.

    Omit ``password`` and the server generates a strong one, returned **once**
    in this response and never stored in readable form. Either way the account
    must change it at first sign-in.
    """
    user, generated = UserService(session).create_user(
        email=str(payload.email),
        full_name=payload.full_name,
        role=payload.role,
        password=payload.password,
        is_active=payload.is_active,
        actor=actor_for(admin, request),
    )
    return CreatedUserResponse(
        user=AdminUserRow.from_user(user),
        temporary_password=generated,
        message=(
            "Share this password over a trusted channel. It is shown once and "
            "the user must change it at first sign-in."
            if generated
            else "The account was created and must change its password at first "
            "sign-in."
        ),
    )


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserRow,
    summary="Edit a user",
    responses={
        403: {"description": "You cannot change your own role or status."},
        404: {"description": "No such user."},
        409: {"description": "That change would remove the last administrator."},
    },
)
def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    request: Request,
    admin: User = Depends(require_permission(Permission.MANAGE_USERS)),
    session: Session = Depends(get_session),
) -> AdminUserRow:
    """
    Change a name, a role, or active status.

    Only the supplied fields are applied. A role or deactivation change also
    revokes that user's sessions, so it takes effect immediately rather than
    whenever their access token happens to expire.
    """
    service = UserService(session)
    user = service.require_user(user_id)
    updated = service.update_user(
        user=user,
        actor=actor_for(admin, request),
        full_name=payload.full_name,
        role=payload.role,
        is_active=payload.is_active,
    )
    return AdminUserRow.from_user(updated)


@router.post(
    "/users/{user_id}/deactivate",
    response_model=AdminUserRow,
    summary="Deactivate a user",
    responses={
        403: {"description": "You cannot deactivate your own account."},
        404: {"description": "No such user."},
        409: {"description": "That would remove the last administrator."},
    },
)
def deactivate_user(
    user_id: str,
    request: Request,
    admin: User = Depends(require_permission(Permission.MANAGE_USERS)),
    session: Session = Depends(get_session),
) -> AdminUserRow:
    """
    Deactivate an account and end its sessions.

    A deactivated account cannot sign in, and its already-issued tokens stop
    working: both the refresh exchange and every request check active status
    against the database rather than trusting the token.
    """
    service = UserService(session)
    user = service.require_user(user_id)
    updated = service.set_active(
        user=user, active=False, actor=actor_for(admin, request)
    )
    return AdminUserRow.from_user(updated)


@router.post(
    "/users/{user_id}/activate",
    response_model=AdminUserRow,
    summary="Reactivate a user",
    responses={404: {"description": "No such user."}},
)
def activate_user(
    user_id: str,
    request: Request,
    admin: User = Depends(require_permission(Permission.MANAGE_USERS)),
    session: Session = Depends(get_session),
) -> AdminUserRow:
    """Re-enable a deactivated account."""
    service = UserService(session)
    user = service.require_user(user_id)
    updated = service.set_active(
        user=user, active=True, actor=actor_for(admin, request)
    )
    return AdminUserRow.from_user(updated)


@router.post(
    "/users/{user_id}/reset-password",
    response_model=ResetPasswordResponse,
    summary="Reset a user's password",
    responses={404: {"description": "No such user."}},
)
def reset_password(
    user_id: str,
    request: Request,
    admin: User = Depends(require_permission(Permission.MANAGE_USERS)),
    session: Session = Depends(get_session),
) -> ResetPasswordResponse:
    """
    Issue a new temporary password.

    Returned once, never stored in readable form, and every session for the
    account is revoked. Any lockout is also cleared, so a reset is a way out of
    one rather than another thing to wait for.
    """
    service = UserService(session)
    user = service.require_user(user_id)
    updated, temporary, revoked = service.reset_password(
        user=user, actor=actor_for(admin, request)
    )
    return ResetPasswordResponse(
        user=AdminUserRow.from_user(updated),
        temporary_password=temporary,
        sessions_revoked=revoked,
    )


__all__ = ["TAG", "router"]
