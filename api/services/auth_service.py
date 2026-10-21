"""
Registration, login, token refresh, logout, and the admin bootstrap.

Design decisions worth stating
------------------------------
**Refresh-token rotation.** Every refresh issues a new refresh token and
revokes the one presented. If a revoked token is presented again, the whole
session family is revoked rather than just that token: either it was replayed
by an attacker or the legitimate client lost the rotation, and both cases are
better handled by forcing a fresh sign-in than by guessing.

**Uniform login failures.** "No such user", "wrong password", and "wrong
password for a user whose email we recognise" all return the same 401 with the
same message. Distinguishing them turns the login form into an
account-enumeration oracle.

**Timing.** A password check runs even when the email does not exist, against a
dummy hash. Without that, the response time alone reveals which addresses are
registered.

**Lockout state lives in the database.** Counters on the user row survive a
restart and are shared between API replicas, which an in-process counter is
not.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from api.core.errors import (
    AccountInactiveError,
    AccountLockedError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    RegistrationDisabledError,
    TokenInvalidError,
    UserNotFoundError,
)
from api.core.rbac import Role
from api.core.security import (
    TokenClaims,
    TokenType,
    create_token,
    decode_token,
    generate_password,
    hash_password,
    hash_token,
    refresh_token_ttl,
    validate_password_strength,
    verify_password,
)
from api.db.base import utcnow
from api.db.models.audit import AuditAction, ResourceType
from api.db.models.user import RefreshToken, User
from api.db.repositories.user_repository import (
    RefreshTokenRepository,
    UserRepository,
    normalise_email,
)
from api.services.audit_service import (
    OUTCOME_DENIED,
    OUTCOME_FAILURE,
    ActorContext,
    AuditService,
)

logger = logging.getLogger(__name__)

_dummy_hash_cache: Optional[str] = None


def _dummy_hash() -> str:
    """
    A real bcrypt hash of an unguessable string.

    Verified against when no user exists, so the "unknown email" path costs the
    same as a genuine password check and the response time does not reveal
    which addresses are registered. Computed once per process and cached: it
    must be a *valid* hash, otherwise bcrypt rejects it immediately and the
    timing equalisation does not happen.
    """
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = hash_password(secrets.token_urlsafe(24))
    return _dummy_hash_cache


DEFAULT_MAX_LOGIN_ATTEMPTS = 8
DEFAULT_LOCKOUT_MINUTES = 15

REVOKE_LOGOUT = "logout"
REVOKE_ROTATED = "rotated"
REVOKE_REUSE_DETECTED = "reuse_detected"
REVOKE_PASSWORD_CHANGED = "password_changed"
REVOKE_DEACTIVATED = "account_deactivated"
REVOKE_ROLE_CHANGED = "role_changed"


@dataclass(frozen=True)
class TokenPair:
    """A freshly issued access/refresh pair."""

    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    token_type: str = "bearer"

    @property
    def expires_in(self) -> int:
        """Access-token lifetime in whole seconds."""
        return max(0, int((self.access_expires_at - utcnow()).total_seconds()))


@dataclass(frozen=True)
class AuthResult:
    """The outcome of a successful authentication."""

    user: User
    tokens: TokenPair


def self_registration_enabled() -> bool:
    """
    Whether ``POST /auth/register`` is open.

    Defaults to **off**: a forecasting platform with an open sign-up form would
    let anyone reach the model. A deployment that wants it says so explicitly.
    """
    return os.getenv("ALLOW_SELF_REGISTRATION", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def default_self_registration_role() -> Role:
    """
    The role a self-registered account receives.

    ``VIEWER`` unless overridden, and never a privileged role: self-registration
    must not be a path to promoting a model. Any configured value above ANALYST
    is refused.
    """
    configured = Role.from_value(os.getenv("SELF_REGISTRATION_ROLE")) or Role.VIEWER
    if configured in (Role.ADMIN, Role.ML_ENGINEER):
        logger.warning(
            "SELF_REGISTRATION_ROLE=%s is too privileged for self-registration; "
            "using VIEWER",
            configured.value,
        )
        return Role.VIEWER
    return configured


def max_login_attempts() -> int:
    """Failed attempts tolerated before a lockout."""
    return _env_int("MAX_LOGIN_ATTEMPTS", DEFAULT_MAX_LOGIN_ATTEMPTS, 3, 100)


def lockout_window() -> timedelta:
    """How long a lockout lasts."""
    return timedelta(
        minutes=_env_int("LOGIN_LOCKOUT_MINUTES", DEFAULT_LOCKOUT_MINUTES, 1, 24 * 60)
    )


class AuthService:
    """Authentication operations over one database session."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.tokens = RefreshTokenRepository(session)
        self.audit = AuditService(session)

    # -- registration ------------------------------------------------------

    def register(
        self,
        *,
        email: str,
        full_name: str,
        password: str,
        actor: ActorContext,
    ) -> AuthResult:
        """
        Create an account through the public endpoint and sign it in.

        Refuses outright when self-registration is disabled, and the role is
        never taken from the request body.
        """
        if not self_registration_enabled():
            self.audit.record(
                action=AuditAction.USER_REGISTERED,
                resource_type=ResourceType.USER,
                actor=actor,
                outcome=OUTCOME_DENIED,
                metadata={"reason": "self-registration disabled"},
            )
            raise RegistrationDisabledError()

        canonical = normalise_email(email)
        validate_password_strength(password, email=canonical)

        user = self.users.create(
            email=canonical,
            full_name=full_name,
            hashed_password=hash_password(password),
            role=default_self_registration_role(),
            is_active=True,
        )

        self.audit.record(
            action=AuditAction.USER_REGISTERED,
            resource_type=ResourceType.USER,
            resource_id=str(user.id),
            actor=ActorContext(
                user_id=user.id,
                email=user.email,
                role=user.role_enum,
                ip_address=actor.ip_address,
                user_agent=actor.user_agent,
                request_id=actor.request_id,
            ),
            metadata={"role": user.role, "self_registered": True},
        )

        tokens = self._issue_tokens(user, actor=actor)
        self.users.record_successful_login(user)
        return AuthResult(user=user, tokens=tokens)

    # -- login -------------------------------------------------------------

    def authenticate(
        self, *, email: str, password: str, actor: ActorContext
    ) -> AuthResult:
        """
        Verify credentials and issue a token pair.

        Order matters: lockout is checked before the password, so hammering a
        locked account cannot be used to keep testing passwords.
        """
        canonical = normalise_email(email)
        user = self.users.get_by_email(canonical)

        if user is None:
            # Spend the same time as a real verification, then fail identically
            # to a wrong password.
            verify_password(password, _dummy_hash())
            self._record_login_failure(actor, canonical, reason="unknown_email")
            raise InvalidCredentialsError()

        if user.is_locked:
            self._record_login_failure(
                actor, canonical, reason="locked", user_id=user.id
            )
            raise AccountLockedError(
                details={"locked_until": user.locked_until.isoformat()}
                if user.locked_until
                else {}
            )

        if not verify_password(password, user.hashed_password):
            self.users.record_failed_login(
                user, max_attempts=max_login_attempts(), lockout=lockout_window()
            )
            self._record_login_failure(
                actor,
                canonical,
                reason="bad_password",
                user_id=user.id,
                attempts=user.failed_login_count,
            )
            raise InvalidCredentialsError()

        if not user.is_active:
            # Checked after the password so a deactivated account is not
            # distinguishable from a wrong password by anyone who lacks the
            # password.
            self._record_login_failure(
                actor, canonical, reason="inactive", user_id=user.id
            )
            raise AccountInactiveError()

        tokens = self._issue_tokens(user, actor=actor)
        self.users.record_successful_login(user)

        self.audit.record(
            action=AuditAction.USER_LOGIN,
            resource_type=ResourceType.SESSION,
            resource_id=str(user.id),
            actor=self._actor_for(user, actor),
            metadata={"role": user.role},
        )
        return AuthResult(user=user, tokens=tokens)

    # -- refresh -----------------------------------------------------------

    def refresh(self, *, refresh_token: str, actor: ActorContext) -> AuthResult:
        """
        Exchange a refresh token for a new pair, rotating the old one.

        A token that is already revoked means either replay or a lost rotation;
        both revoke every session for that user, because continuing with an
        unknown cause is the worse option.
        """
        if not refresh_token:
            raise NotAuthenticatedError("A refresh token is required.")

        claims = decode_token(refresh_token, TokenType.REFRESH)
        stored = self.tokens.get_by_hash(hash_token(refresh_token))

        if stored is None:
            # Correctly signed but unknown to us: issued before a secret
            # rotation, or forged with a leaked secret. Either way, not usable.
            logger.warning("Refresh token %s is not on record", claims.jti)
            raise TokenInvalidError("This session is no longer valid.")

        if stored.is_revoked:
            revoked = self.tokens.revoke_all_for_user(
                stored.user_id, reason=REVOKE_REUSE_DETECTED
            )
            logger.warning(
                "Revoked refresh token %s was replayed; invalidated %d session(s) "
                "for user %s",
                claims.jti,
                revoked,
                stored.user_id,
            )
            raise TokenInvalidError("This session is no longer valid.")

        if stored.is_expired:
            self.tokens.revoke(stored, reason="expired")
            raise TokenInvalidError("This session has expired.")

        user = self.users.get(stored.user_id)
        if user is None:
            self.tokens.revoke(stored, reason="user_missing")
            raise TokenInvalidError("This session is no longer valid.")
        if not user.is_active:
            # Deactivation must take effect for already-issued tokens, which is
            # exactly what this check provides.
            self.tokens.revoke_all_for_user(user.id, reason=REVOKE_DEACTIVATED)
            raise AccountInactiveError()

        new_tokens = self._issue_tokens(user, actor=actor)
        new_claims_jti = decode_token(new_tokens.refresh_token, TokenType.REFRESH).jti
        self.tokens.revoke(
            stored, reason=REVOKE_ROTATED, replaced_by_jti=new_claims_jti
        )

        self.audit.record(
            action=AuditAction.TOKEN_REFRESHED,
            resource_type=ResourceType.SESSION,
            resource_id=str(user.id),
            actor=self._actor_for(user, actor),
            metadata={"rotated_from": claims.jti},
        )
        return AuthResult(user=user, tokens=new_tokens)

    # -- logout ------------------------------------------------------------

    def logout(
        self,
        *,
        refresh_token: Optional[str],
        actor: ActorContext,
        all_sessions: bool = False,
    ) -> int:
        """
        Revoke the presented session, or every session for the actor.

        Returns the number of sessions revoked. Succeeds even with no usable
        token: logout must always leave the caller signed out, and a client that
        has lost its token still needs the call to work.
        """
        revoked = 0

        if all_sessions and actor.user_id is not None:
            revoked = self.tokens.revoke_all_for_user(
                actor.user_id, reason=REVOKE_LOGOUT
            )
        elif refresh_token:
            try:
                stored = self.tokens.get_by_hash(hash_token(refresh_token))
            except Exception as exc:  # noqa: BLE001 - logout must not fail
                logger.warning("Could not look up a refresh token on logout: %s", exc)
                stored = None
            if stored is not None and not stored.is_revoked:
                self.tokens.revoke(stored, reason=REVOKE_LOGOUT)
                revoked = 1

        self.audit.record(
            action=AuditAction.USER_LOGOUT,
            resource_type=ResourceType.SESSION,
            resource_id=str(actor.user_id) if actor.user_id else None,
            actor=actor,
            metadata={"sessions_revoked": revoked, "all_sessions": all_sessions},
        )
        return revoked

    # -- password ----------------------------------------------------------

    def change_password(
        self,
        *,
        user: User,
        current_password: str,
        new_password: str,
        actor: ActorContext,
    ) -> User:
        """
        Change a user's own password and end every other session.

        Revoking sessions is the point: a password change that leaves old
        sessions alive does not actually lock anyone out.
        """
        if not verify_password(current_password, user.hashed_password):
            self.audit.record(
                action=AuditAction.PASSWORD_CHANGED,
                resource_type=ResourceType.USER,
                resource_id=str(user.id),
                actor=actor,
                outcome=OUTCOME_FAILURE,
                metadata={"reason": "current password incorrect"},
            )
            raise InvalidCredentialsError("Your current password is incorrect.")

        validate_password_strength(new_password, email=user.email)
        self.users.update(
            user,
            hashed_password=hash_password(new_password),
            must_change_password=False,
        )
        revoked = self.tokens.revoke_all_for_user(
            user.id, reason=REVOKE_PASSWORD_CHANGED
        )

        self.audit.record(
            action=AuditAction.PASSWORD_CHANGED,
            resource_type=ResourceType.USER,
            resource_id=str(user.id),
            actor=actor,
            metadata={"sessions_revoked": revoked},
        )
        return user

    def bootstrap_service_account(self) -> Optional[Tuple[User, bool]]:
        """
        Create the monitoring service account, if one is configured.

        Returns ``(user, created)``, or ``None`` when no password is set.

        The monitoring DAG authenticates as a normal account rather than with a
        bypass or a shared API key, so its writes are attributable in the audit
        log exactly like a person's. It gets ML_ENGINEER -- enough to evaluate
        monitoring and manage alerts, not enough to manage users.

        Idempotent: an existing account has its password reset to the
        configured value so a rotation takes effect, but its role is left
        alone in case an administrator deliberately changed it.
        """
        password = os.getenv("MONITORING_SERVICE_PASSWORD", "").strip()
        if not password:
            return None

        email = normalise_email(
            os.getenv("MONITORING_SERVICE_EMAIL", "monitoring-service@example.com")
        )
        existing = self.users.get_by_email(email)

        if existing is not None:
            self.users.update(
                existing,
                hashed_password=hash_password(password),
                # A service account cannot complete an interactive password
                # change, so it must never be flagged as needing one.
                must_change_password=False,
                is_active=True,
            )
            return existing, False

        user = self.users.create(
            email=email,
            full_name=os.getenv(
                "MONITORING_SERVICE_NAME", "Monitoring Service"
            ),
            hashed_password=hash_password(password),
            role=Role.ML_ENGINEER,
            is_active=True,
            must_change_password=False,
        )
        self.audit.record(
            action=AuditAction.USER_CREATED,
            resource_type=ResourceType.USER,
            resource_id=str(user.id),
            actor=ActorContext(email=email, role=Role.ML_ENGINEER),
            metadata={"service_account": True, "role": Role.ML_ENGINEER.value},
        )
        logger.info("Created the monitoring service account %s", email)
        return user, True

    # -- bootstrap ---------------------------------------------------------

    def bootstrap_admin(self) -> Optional[Tuple[User, Optional[str]]]:
        """
        Create the first ADMIN account when the user table is empty.

        Returns ``(user, generated_password_or_None)``, or ``None`` when
        accounts already exist. Only ever runs against an empty table, so it
        cannot overwrite or re-privilege an existing account.

        When ``BOOTSTRAP_ADMIN_PASSWORD`` is unset a strong password is
        generated and returned for the caller to log once -- better than a
        known default that nobody changes.
        """
        if self.users.count() > 0:
            return None

        email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com").strip().lower()
        full_name = os.getenv("BOOTSTRAP_ADMIN_NAME", "Platform Administrator").strip()
        configured_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "").strip()

        generated: Optional[str] = None
        if configured_password:
            try:
                validate_password_strength(configured_password, email=email)
                password = configured_password
            except Exception as exc:  # noqa: BLE001 - fall back, do not block boot
                logger.error(
                    "BOOTSTRAP_ADMIN_PASSWORD does not meet the password policy "
                    "(%s); generating one instead",
                    exc,
                )
                password = generate_password()
                generated = password
        else:
            password = generate_password()
            generated = password

        user = self.users.create(
            email=email,
            full_name=full_name or "Platform Administrator",
            hashed_password=hash_password(password),
            role=Role.ADMIN,
            is_active=True,
            # A generated or shared bootstrap credential must not stay in use.
            must_change_password=True,
        )

        self.audit.record(
            action=AuditAction.USER_CREATED,
            resource_type=ResourceType.USER,
            resource_id=str(user.id),
            actor=ActorContext(email=email, role=Role.ADMIN),
            metadata={"bootstrap": True, "role": Role.ADMIN.value},
        )
        logger.info("Created the bootstrap ADMIN account for %s", email)
        return user, generated

    # -- helpers -----------------------------------------------------------

    def _issue_tokens(self, user: User, *, actor: ActorContext) -> TokenPair:
        """Mint an access/refresh pair and record the refresh token."""
        access_token, access_claims = create_token(
            subject=str(user.id),
            token_type=TokenType.ACCESS,
            role=user.role,
            email=user.email,
        )
        refresh_token, refresh_claims = create_token(
            subject=str(user.id),
            token_type=TokenType.REFRESH,
            role=user.role,
            email=user.email,
        )

        self.tokens.create(
            jti=refresh_claims.jti,
            token_hash=hash_token(refresh_token),
            user_id=user.id,
            expires_at=refresh_claims.expires_at,
            user_agent=actor.user_agent,
            ip_address=actor.ip_address,
        )

        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_claims.expires_at,
            refresh_expires_at=refresh_claims.expires_at,
        )

    def _record_login_failure(
        self,
        actor: ActorContext,
        email: str,
        *,
        reason: str,
        user_id: Optional[uuid.UUID] = None,
        attempts: Optional[int] = None,
    ) -> None:
        """Record a failed sign-in. The password is never part of the record."""
        metadata = {"reason": reason, "email": email}
        if attempts is not None:
            metadata["failed_attempts"] = attempts
        self.audit.record(
            action=AuditAction.USER_LOGIN_FAILED,
            resource_type=ResourceType.SESSION,
            resource_id=str(user_id) if user_id else None,
            actor=actor,
            outcome=OUTCOME_FAILURE,
            metadata=metadata,
        )

    @staticmethod
    def _actor_for(user: User, request_actor: ActorContext) -> ActorContext:
        """Actor context for an authenticated user, keeping the request detail."""
        return ActorContext(
            user_id=user.id,
            email=user.email,
            role=user.role_enum,
            ip_address=request_actor.ip_address,
            user_agent=request_actor.user_agent,
            request_id=request_actor.request_id,
        )


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """A bounded integer from the environment."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


__all__ = [
    "AuthResult",
    "AuthService",
    "TokenPair",
    "default_self_registration_role",
    "lockout_window",
    "max_login_attempts",
    "self_registration_enabled",
]
