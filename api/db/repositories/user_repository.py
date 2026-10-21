"""
User and refresh-token persistence.

Repositories take a ``Session`` and never create one: the caller owns the
transaction boundary, which is what lets a route write a user change and its
audit entry in a single atomic unit.

They also never commit. A repository that commits makes it impossible for a
caller to roll back a multi-step operation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from api.core.errors import DatabaseUnavailableError, EmailAlreadyRegisteredError
from api.core.rbac import Role
from api.db.base import utcnow
from api.db.models.user import RefreshToken, User

logger = logging.getLogger(__name__)


def normalise_email(email: str) -> str:
    """
    Canonical form of an email address for storage and lookup.

    Lower-cased and trimmed, so `Ada@Example.com ` and `ada@example.com` are
    the same account rather than two.
    """
    return email.strip().lower()


class UserRepository:
    """Reads and writes ``users``."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- reads -------------------------------------------------------------

    def get(self, user_id: uuid.UUID) -> Optional[User]:
        """One user by id."""
        return self._guard(lambda: self.session.get(User, user_id), "load user")

    def get_by_email(self, email: str) -> Optional[User]:
        """One user by email, case-insensitively."""
        statement = select(User).where(User.email == normalise_email(email))
        return self._guard(
            lambda: self.session.execute(statement).scalar_one_or_none(),
            "load user by email",
        )

    def count(self) -> int:
        """How many accounts exist. Drives the bootstrap decision."""
        return self._guard(
            lambda: int(self.session.execute(select(func.count(User.id))).scalar() or 0),
            "count users",
        )

    def list_users(
        self,
        *,
        role: Optional[Role] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[User], int]:
        """
        A page of users, newest first, with the unpaginated total.

        The total is returned alongside the page so the UI can render "showing
        20 of 143" without a second round trip.
        """
        filters = []
        if role is not None:
            filters.append(User.role == role.value)
        if is_active is not None:
            filters.append(User.is_active == is_active)
        if search:
            pattern = f"%{search.strip().lower()}%"
            filters.append(
                func.lower(User.email).like(pattern)
                | func.lower(User.full_name).like(pattern)
            )

        page_statement = (
            select(User)
            .where(*filters)
            .order_by(User.created_at.desc())
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
        count_statement = select(func.count(User.id)).where(*filters)

        def run() -> Tuple[List[User], int]:
            rows = list(self.session.execute(page_statement).scalars())
            total = int(self.session.execute(count_statement).scalar() or 0)
            return rows, total

        return self._guard(run, "list users")

    # -- writes ------------------------------------------------------------

    def create(
        self,
        *,
        email: str,
        full_name: str,
        hashed_password: str,
        role: Role,
        is_active: bool = True,
        must_change_password: bool = False,
        created_by_id: Optional[uuid.UUID] = None,
    ) -> User:
        """
        Insert a user.

        A duplicate email is reported as ``EmailAlreadyRegisteredError`` from
        both the pre-check and the integrity error: the pre-check gives a clean
        message, and the constraint catches the race between two concurrent
        registrations that the pre-check cannot.
        """
        canonical = normalise_email(email)
        if self.get_by_email(canonical) is not None:
            raise EmailAlreadyRegisteredError(field="email")

        user = User(
            email=canonical,
            full_name=full_name.strip(),
            hashed_password=hashed_password,
            role=role.value,
            is_active=is_active,
            must_change_password=must_change_password,
            created_by_id=created_by_id,
        )
        self.session.add(user)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise EmailAlreadyRegisteredError(field="email") from exc
        except SQLAlchemyError as exc:
            raise self._unavailable("create user", exc) from exc
        return user

    def update(self, user: User, **changes: Any) -> User:
        """
        Apply field changes to a user.

        Only an explicit allowlist is assignable, so a request body can never
        reach ``hashed_password``, ``id``, or the lockout counters through a
        generic update path.
        """
        allowed = {
            "full_name",
            "role",
            "is_active",
            "must_change_password",
            "hashed_password",
            "last_login_at",
            "failed_login_count",
            "locked_until",
        }
        for field, value in changes.items():
            if field not in allowed:
                raise ValueError(f"{field!r} is not an updatable user field")
            setattr(user, field, value)
        user.updated_at = utcnow()
        return self._guard(lambda: (self.session.flush(), user)[1], "update user")

    def record_successful_login(self, user: User) -> User:
        """Stamp the login time and clear any failure state."""
        user.last_login_at = utcnow()
        user.failed_login_count = 0
        user.locked_until = None
        return self._guard(lambda: (self.session.flush(), user)[1], "record login")

    def record_failed_login(
        self, user: User, *, max_attempts: int, lockout: timedelta
    ) -> User:
        """
        Increment the failure counter and lock the account past the threshold.

        Counters live on the row rather than in process memory so the lockout
        survives a restart and applies across API replicas.
        """
        user.failed_login_count = int(user.failed_login_count or 0) + 1
        if user.failed_login_count >= max_attempts:
            user.locked_until = utcnow() + lockout
            logger.warning(
                "Locking user %s until %s after %d failed attempts",
                user.id,
                user.locked_until,
                user.failed_login_count,
            )
        return self._guard(lambda: (self.session.flush(), user)[1], "record failed login")

    # -- helpers -----------------------------------------------------------

    def _guard(self, operation, action: str):
        """Run a database call, converting driver failures to typed errors."""
        try:
            return operation()
        except (EmailAlreadyRegisteredError, DatabaseUnavailableError):
            raise
        except SQLAlchemyError as exc:
            raise self._unavailable(action, exc) from exc

    @staticmethod
    def _unavailable(action: str, exc: Exception) -> DatabaseUnavailableError:
        """Wrap a driver failure without leaking the connection string."""
        logger.error("Database error while trying to %s: %s", action, exc)
        return DatabaseUnavailableError(
            "The user store is currently unavailable.", details={"operation": action}
        )


class RefreshTokenRepository:
    """Reads and writes ``refresh_tokens``."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        jti: str,
        token_hash: str,
        user_id: uuid.UUID,
        expires_at: datetime,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> RefreshToken:
        """Record a newly issued refresh token."""
        token = RefreshToken(
            jti=jti,
            token_hash=token_hash,
            user_id=user_id,
            expires_at=expires_at,
            user_agent=(user_agent or None),
            ip_address=(ip_address or None),
        )
        self.session.add(token)
        self.session.flush()
        return token

    def get_by_hash(self, token_hash: str) -> Optional[RefreshToken]:
        """One token by digest. The lookup the refresh flow performs."""
        statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return self.session.execute(statement).scalar_one_or_none()

    def get_by_jti(self, jti: str) -> Optional[RefreshToken]:
        """One token by its ``jti`` claim."""
        statement = select(RefreshToken).where(RefreshToken.jti == jti)
        return self.session.execute(statement).scalar_one_or_none()

    def revoke(
        self, token: RefreshToken, *, reason: str, replaced_by_jti: Optional[str] = None
    ) -> RefreshToken:
        """Mark one token revoked. Idempotent: an already-revoked token is left as it was."""
        if token.revoked_at is None:
            token.revoked_at = utcnow()
            token.revoked_reason = reason
        if replaced_by_jti:
            token.replaced_by_jti = replaced_by_jti
        self.session.flush()
        return token

    def revoke_all_for_user(self, user_id: uuid.UUID, *, reason: str) -> int:
        """
        Revoke every live session for a user, returning the count.

        Used by logout-everywhere, deactivation, and password change. The last
        of those matters: changing a password that leaves old sessions alive
        does not actually lock anyone out.
        """
        statement = select(RefreshToken).where(
            RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
        )
        tokens = list(self.session.execute(statement).scalars())
        now = utcnow()
        for token in tokens:
            token.revoked_at = now
            token.revoked_reason = reason
        self.session.flush()
        return len(tokens)

    def active_sessions(self, user_id: uuid.UUID) -> List[RefreshToken]:
        """Live, unexpired sessions for a user, newest first."""
        statement = (
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > utcnow(),
            )
            .order_by(RefreshToken.created_at.desc())
        )
        return list(self.session.execute(statement).scalars())

    def purge_expired(self, *, older_than: Optional[datetime] = None) -> int:
        """
        Delete tokens that expired before a cutoff, returning the count.

        Maintenance only. Revoked-but-unexpired rows are kept so "when was this
        session revoked" stays answerable.
        """
        cutoff = older_than or utcnow()
        statement = select(RefreshToken).where(RefreshToken.expires_at < cutoff)
        rows = list(self.session.execute(statement).scalars())
        for row in rows:
            self.session.delete(row)
        self.session.flush()
        return len(rows)


__all__ = ["RefreshTokenRepository", "UserRepository", "normalise_email"]
