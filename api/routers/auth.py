"""
Authentication routes.

Thin: parse, delegate to ``AuthService``, set or clear the refresh cookie.
Every failure is a taxonomy error rendered by the installed boundary.

Refresh-token transport
-----------------------
The refresh token is set as an **HttpOnly, SameSite=Lax** cookie scoped to
``/auth``. JavaScript cannot read it, so an XSS payload cannot exfiltrate it,
and it is only sent to the three routes that need it. The access token goes in
the response body for the SPA to hold in memory, where a page reload discards
it -- which is the point of pairing a short-lived access token with a
cookie-held refresh token.

``COOKIE_SECURE`` defaults to on and must be turned off explicitly for plain
HTTP local development; a secure-by-default cookie that silently fails over
http is better than an insecure-by-default one that ships to production.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from api.core.errors import NotAuthenticatedError
from api.core.rbac import ROLE_ORDER
from api.core.security import MIN_PASSWORD_LENGTH, refresh_token_ttl
from api.db.models.user import User
from api.db.repositories.user_repository import RefreshTokenRepository
from api.db.session import get_session
from api.dependencies.auth import (
    REFRESH_COOKIE_NAME,
    get_actor,
    get_current_user,
)
from api.dependencies.auth import actor_for
from api.schemas.auth import (
    AuthConfigResponse,
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    RefreshRequest,
    RegisterRequest,
    SessionSummary,
    TokenResponse,
    UserProfile,
)
from api.services.audit_service import ActorContext
from api.services.auth_service import (
    AuthResult,
    AuthService,
    TokenPair,
    default_self_registration_role,
    self_registration_enabled,
    self_registration_roles,
)

logger = logging.getLogger(__name__)

TAG = "Authentication"

router = APIRouter(prefix="/auth", tags=[TAG])

#: Scoped to /auth so the cookie is not attached to every API call. A token
#: that is sent on requests which cannot use it is a token with a larger
#: exposure surface than it needs.
COOKIE_PATH = "/auth"


def cookie_secure() -> bool:
    """Whether to mark the refresh cookie ``Secure``. Defaults to on."""
    return os.getenv("COOKIE_SECURE", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def cookie_samesite() -> str:
    """
    ``SameSite`` policy for the refresh cookie.

    ``lax`` suits a frontend on the same site as the API (localhost:3000 ->
    localhost:8000 is same-site: the port is not part of a site). A deployment
    that puts them on different domains needs ``none``, which browsers only
    honour together with ``Secure``.
    """
    value = os.getenv("COOKIE_SAMESITE", "lax").strip().lower()
    return value if value in ("lax", "strict", "none") else "lax"


def return_refresh_in_body() -> bool:
    """
    Whether to also return the refresh token in the response body.

    Off by default. A non-browser client with no cookie jar can turn it on, at
    the cost of the token being readable by anything that can read the
    response.
    """
    return os.getenv("AUTH_REFRESH_TOKEN_IN_BODY", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def set_refresh_cookie(response: Response, tokens: TokenPair) -> None:
    """Attach the refresh token as an HttpOnly cookie."""
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=tokens.refresh_token,
        max_age=int(refresh_token_ttl().total_seconds()),
        path=COOKIE_PATH,
        httponly=True,
        secure=cookie_secure(),
        samesite=cookie_samesite(),
    )


def clear_refresh_cookie(response: Response) -> None:
    """Remove the refresh cookie. Attributes must match the ones used to set it."""
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=COOKIE_PATH,
        httponly=True,
        secure=cookie_secure(),
        samesite=cookie_samesite(),
    )


def _token_response(result: AuthResult) -> TokenResponse:
    """Build the body for a successful authentication."""
    return TokenResponse(
        access_token=result.tokens.access_token,
        expires_in=result.tokens.expires_in,
        user=UserProfile.from_user(result.user),
        refresh_token=(
            result.tokens.refresh_token if return_refresh_in_body() else None
        ),
    )


def _presented_refresh_token(
    request: Request, payload: Optional[RefreshRequest]
) -> Optional[str]:
    """
    The refresh token from the cookie, falling back to the body.

    Cookie first: a browser always has it there, and preferring the body would
    let a caller substitute a different session's token.
    """
    cookie = request.cookies.get(REFRESH_COOKIE_NAME)
    if cookie:
        return cookie
    return payload.refresh_token if payload else None


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------


@router.get("/config", response_model=AuthConfigResponse, summary="Login page configuration")
def auth_config() -> AuthConfigResponse:
    """
    What the login page needs before anyone has authenticated.

    Public by necessity -- it is read to decide whether to render the
    registration link -- and carries nothing sensitive.
    """
    return AuthConfigResponse(
        self_registration_enabled=self_registration_enabled(),
        password_min_length=MIN_PASSWORD_LENGTH,
        roles=[role.value for role in ROLE_ORDER],
        self_registration_roles=[role.value for role in self_registration_roles()],
        default_self_registration_role=default_self_registration_role().value,
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=201,
    summary="Create an account (when self-registration is enabled)",
    responses={
        403: {
            "description": (
                "Self-registration is disabled, or the requested role cannot be "
                "chosen at sign-up."
            )
        },
        409: {"description": "That email address is already registered."},
        422: {"description": "The password does not meet the policy."},
    },
)
def register(
    payload: RegisterRequest,
    response: Response,
    request: Request,
    session: Session = Depends(get_session),
) -> TokenResponse:
    """
    Register and sign in.

    Returns 403 when self-registration is disabled, which is the default: an
    open sign-up form on a forecasting platform would let anyone reach the
    model. A requested role is granted only if this deployment lists it as
    self-assignable (see ``GET /auth/config``).
    """
    result = AuthService(session).register(
        email=str(payload.email),
        full_name=payload.full_name,
        password=payload.password,
        actor=get_actor(request),
        role=payload.role,
    )
    set_refresh_cookie(response, result.tokens)
    return _token_response(result)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in",
    responses={
        401: {"description": "Incorrect email or password."},
        403: {"description": "The account has been deactivated."},
        423: {"description": "Too many failed attempts; the account is locked."},
    },
)
def login(
    payload: LoginRequest,
    response: Response,
    request: Request,
    session: Session = Depends(get_session),
) -> TokenResponse:
    """
    Exchange credentials for an access token and a refresh cookie.

    A wrong password and an unknown address return the identical 401: telling
    them apart would turn this route into an account-enumeration oracle.
    """
    result = AuthService(session).authenticate(
        email=str(payload.email),
        password=payload.password,
        actor=get_actor(request),
    )
    set_refresh_cookie(response, result.tokens)
    return _token_response(result)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for a new access token",
    responses={
        401: {"description": "The session is missing, expired, or no longer valid."},
        403: {"description": "The account has been deactivated."},
    },
)
def refresh(
    response: Response,
    request: Request,
    payload: Optional[RefreshRequest] = None,
    session: Session = Depends(get_session),
) -> TokenResponse:
    """
    Rotate the session.

    The presented token is revoked and replaced. Presenting an
    already-rotated token revokes every session for that account: it means
    either replay or a lost rotation, and a forced re-authentication is the
    safe response to both.
    """
    token = _presented_refresh_token(request, payload)
    if not token:
        clear_refresh_cookie(response)
        raise NotAuthenticatedError("No session to refresh.")

    try:
        result = AuthService(session).refresh(
            refresh_token=token, actor=get_actor(request)
        )
    except Exception:
        # A dead cookie would otherwise be replayed on every page load,
        # producing a stream of 401s the user cannot clear.
        clear_refresh_cookie(response)
        raise

    set_refresh_cookie(response, result.tokens)
    return _token_response(result)


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="Sign out",
)
def logout(
    response: Response,
    request: Request,
    payload: Optional[LogoutRequest] = None,
    session: Session = Depends(get_session),
) -> LogoutResponse:
    """
    Revoke the current session, or every session for the account.

    Public, and always succeeds. Logout must leave the caller signed out even
    when their token has already expired -- failing here would strand a client
    that cannot clear its own state.
    """
    token = _presented_refresh_token(request, payload)
    all_sessions = bool(payload.all_sessions) if payload else False

    # Identify the actor from the refresh token when possible, so the audit
    # record is attributed even though this route does not require a session.
    actor = get_actor(request)
    service = AuthService(session)
    if token:
        try:
            from api.core.security import TokenType, decode_token

            claims = decode_token(token, TokenType.REFRESH)
            user = service.users.get(claims.user_id)
            if user is not None:
                actor = actor_for(user, request)
        except Exception as exc:  # noqa: BLE001 - attribution is best-effort
            logger.debug("Could not attribute a logout: %s", exc)

    revoked = service.logout(
        refresh_token=token, actor=actor, all_sessions=all_sessions
    )
    clear_refresh_cookie(response)
    return LogoutResponse(sessions_revoked=revoked)


# ---------------------------------------------------------------------------
# Authenticated routes
# ---------------------------------------------------------------------------


@router.get("/me", response_model=UserProfile, summary="The current user")
def me(user: User = Depends(get_current_user)) -> UserProfile:
    """
    The signed-in user and the permissions their role grants.

    The frontend calls this on load to decide what to render. The permission
    list is presentation only; the API re-checks every call.
    """
    return UserProfile.from_user(user)


@router.post(
    "/change-password",
    response_model=UserProfile,
    summary="Change your own password",
    responses={
        401: {"description": "The current password is incorrect."},
        422: {"description": "The new password does not meet the policy."},
    },
)
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> UserProfile:
    """
    Change the signed-in user's password.

    Every session is revoked, including this one, so the client must sign in
    again. A password change that left old sessions alive would not actually
    lock anyone out.
    """
    updated = AuthService(session).change_password(
        user=user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        actor=actor_for(user, request),
    )
    clear_refresh_cookie(response)
    return UserProfile.from_user(updated)


@router.get(
    "/sessions",
    response_model=list[SessionSummary],
    summary="Your live sessions",
)
def sessions(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[SessionSummary]:
    """
    Live sessions for the signed-in user.

    Lets someone see where their account is signed in, which is the
    prerequisite for noticing a session they do not recognise.
    """
    from api.core.security import hash_token

    current_cookie = request.cookies.get(REFRESH_COOKIE_NAME)
    current_hash = hash_token(current_cookie) if current_cookie else None

    rows = RefreshTokenRepository(session).active_sessions(user.id)
    return [
        SessionSummary(
            id=str(row.id),
            created_at=row.created_at,
            expires_at=row.expires_at,
            ip_address=row.ip_address,
            user_agent=row.user_agent,
            current=bool(current_hash and row.token_hash == current_hash),
        )
        for row in rows
    ]


__all__ = ["TAG", "router"]
