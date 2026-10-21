"""
Authentication and authorization dependencies.

Every protected route declares what it needs as a dependency, so the rule is
visible in the signature rather than buried in the body:

    @router.post("/promote")
    def promote(user: User = Depends(require_permission(Permission.PROMOTE_MODEL))):
        ...

Three layers, deliberately separate:

* ``get_current_user`` -- "who is this?" (401 on failure)
* ``require_permission`` -- "may they?" (403 on failure)
* ``get_actor`` -- the context every audit record needs

The user is re-loaded from the database on each request rather than trusted
from the token. A token minted five minutes ago cannot know that the account
was deactivated or demoted since, and treating its ``role`` claim as
authoritative would mean a revoked privilege survived for the token's whole
lifetime.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, List, Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from api.core.errors import (
    AccountInactiveError,
    NotAuthenticatedError,
    PermissionDeniedError,
    TokenInvalidError,
)
from api.core.rbac import Permission, Role, has_permission
from api.core.security import TokenType, decode_token
from api.db.models.user import User
from api.db.repositories.user_repository import UserRepository
from api.db.session import get_session
from api.middleware.error_handler import get_request_id
from api.services.audit_service import ActorContext, AuditService

logger = logging.getLogger(__name__)

#: Name of the HttpOnly cookie carrying the refresh token.
REFRESH_COOKIE_NAME = "sf_refresh_token"

#: ``auto_error=False`` so a missing header raises the documented 401 envelope
#: rather than FastAPI's own ``{"detail": "Not authenticated"}`` shape.
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAccessToken")


def client_ip(request: Request) -> Optional[str]:
    """
    Best-effort client address.

    ``X-Forwarded-For`` is honoured because the API sits behind a proxy in any
    real deployment, and its left-most entry is the original client. It is
    attacker-controlled when there is no trusted proxy, so it is only ever used
    for audit context -- never for an authorization decision.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()[:64]
    return request.client.host[:64] if request.client else None


def get_actor(request: Request) -> ActorContext:
    """
    Request context for an unauthenticated action.

    Used by the login and registration routes, which must be able to audit a
    failure before any user is known.
    """
    return ActorContext.anonymous(
        ip_address=client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        request_id=get_request_id(),
    )


def _access_token_from(
    request: Request, credentials: Optional[HTTPAuthorizationCredentials]
) -> Optional[str]:
    """
    Extract the access token from the request.

    The ``Authorization`` header is the documented transport. A cookie is
    accepted as a fallback so a server-rendered page can be protected without
    JavaScript attaching the header.
    """
    if credentials and credentials.scheme.lower() == "bearer" and credentials.credentials:
        return credentials.credentials
    cookie = request.cookies.get("sf_access_token")
    return cookie or None


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    session: Session = Depends(get_session),
) -> User:
    """
    The authenticated user, or a 401/403.

    Rejects a refresh token presented as a bearer token: the ``typ`` claim is
    checked, so a long-lived refresh token cannot be used to call every
    endpoint and defeat the short access-token lifetime.
    """
    token = _access_token_from(request, credentials)
    if not token:
        raise NotAuthenticatedError()

    claims = decode_token(token, TokenType.ACCESS)

    user = UserRepository(session).get(claims.user_id)
    if user is None:
        # Correctly signed token for an account that no longer exists.
        logger.info("Rejected a token for a missing user %s", claims.subject)
        raise TokenInvalidError("This session is no longer valid.")
    if not user.is_active:
        raise AccountInactiveError()
    if user.role_enum is None:
        # An unrecognised stored role must fail closed, not fall through to an
        # empty permission set that silently denies everything with a 403.
        logger.error("User %s has an unrecognised role %r", user.id, user.role)
        raise PermissionDeniedError("Your account's role is not recognised.")

    return user


def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    session: Session = Depends(get_session),
) -> Optional[User]:
    """
    The authenticated user if there is one, otherwise ``None``.

    For routes that are public but behave better with attribution. Never
    raises: an invalid token is treated as no token.
    """
    try:
        return get_current_user(request, credentials, session)
    except Exception:  # noqa: BLE001 - absence of auth is the expected case
        return None


def actor_for(user: User, request: Request) -> ActorContext:
    """Audit context for an authenticated request."""
    return ActorContext(
        user_id=user.id,
        email=user.email,
        role=user.role_enum,
        ip_address=client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        request_id=get_request_id(),
    )


def get_current_actor(
    request: Request, user: User = Depends(get_current_user)
) -> ActorContext:
    """Audit context dependency for a protected route."""
    return actor_for(user, request)


def require_permission(*permissions: Permission) -> Callable[..., User]:
    """
    Build a dependency that requires every listed permission.

    A denial is audited before the 403 is raised: a pattern of refused
    privileged attempts is exactly the kind of thing worth being able to look
    up later, and it also surfaces a mis-assigned role rather than leaving a
    colleague quietly stuck.
    """
    required: List[Permission] = list(permissions)

    def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> User:
        role = user.role_enum
        missing = [item for item in required if not has_permission(role, item)]
        if missing:
            AuditService(session).record_denial(
                actor=actor_for(user, request),
                permission=missing[0],
                resource_id=str(request.url.path),
            )
            raise PermissionDeniedError(
                "You do not have permission to perform this action.",
                details={
                    "required": [item.value for item in missing],
                    "your_role": role.value if role else None,
                },
            )
        return user

    return dependency


def require_role(*roles: Role) -> Callable[..., User]:
    """
    Build a dependency that requires one of a set of roles.

    Prefer ``require_permission``: a capability check reads as its own
    documentation and survives a change to the role set. This exists for the
    few places where the role itself is the subject -- an admin-only page, for
    instance.
    """
    allowed = set(roles)

    def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> User:
        if user.role_enum not in allowed:
            AuditService(session).record_denial(
                actor=actor_for(user, request),
                permission=Permission.MANAGE_USERS,
                resource_id=str(request.url.path),
            )
            raise PermissionDeniedError(
                "You do not have permission to access this area.",
                details={
                    "required_roles": sorted(item.value for item in allowed),
                    "your_role": user.role if user.role else None,
                },
            )
        return user

    return dependency


def any_permission(*permissions: Permission) -> Callable[..., User]:
    """Build a dependency satisfied by *any* of the listed permissions."""
    options = list(permissions)

    def dependency(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ) -> User:
        role = user.role_enum
        if not any(has_permission(role, item) for item in options):
            AuditService(session).record_denial(
                actor=actor_for(user, request),
                permission=options[0],
                resource_id=str(request.url.path),
            )
            raise PermissionDeniedError(
                "You do not have permission to perform this action.",
                details={
                    "required_any_of": [item.value for item in options],
                    "your_role": role.value if role else None,
                },
            )
        return user

    return dependency


__all__ = [
    "REFRESH_COOKIE_NAME",
    "actor_for",
    "any_permission",
    "client_ip",
    "get_actor",
    "get_current_actor",
    "get_current_user",
    "get_optional_user",
    "require_permission",
    "require_role",
]
