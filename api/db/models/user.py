"""
User accounts and refresh tokens.

Two tables. ``users`` is the identity of record; ``refresh_tokens`` is session
state, which is deliberately separate so revoking a session never touches the
account row and a session table can be pruned independently.

What is *not* stored: plaintext passwords, and the refresh tokens themselves.
The password column holds a bcrypt hash and the token column holds a SHA-256
digest, so neither is recoverable from a database dump.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.core.rbac import Role
from api.db.base import TIMESTAMP_TYPE, Base, as_utc, utcnow

USERS_TABLE = "users"
REFRESH_TOKENS_TABLE = "refresh_tokens"

EMAIL_MAX_LENGTH = 320  # the practical maximum for an address
NAME_MAX_LENGTH = 200


class User(Base):
    """A person who can sign in."""

    __tablename__ = USERS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    # Stored lower-cased by the repository so uniqueness is case-insensitive:
    # `Ada@example.com` and `ada@example.com` must not be two accounts.
    email: Mapped[str] = mapped_column(
        String(EMAIL_MAX_LENGTH), nullable=False, unique=True, index=True
    )
    full_name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default=Role.VIEWER.value, index=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )

    #: Set when an administrator creates the account or resets the password, so
    #: the UI can require a change before anything else is done.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    #: Failed-login counters live on the user row rather than in memory so the
    #: lockout survives a restart and is shared across API replicas.
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP_TYPE, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP_TYPE, nullable=True
    )

    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid, ForeignKey(f"{USERS_TABLE}.id", ondelete="SET NULL"), nullable=True
    )

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        # The admin user list is sorted by creation date and filtered by role
        # and status; one composite index covers both.
        Index("ix_users_role_active", "role", "is_active"),
        Index("ix_users_created_at", "created_at"),
    )

    @property
    def role_enum(self) -> Optional[Role]:
        """The role as an enum, or None if the stored value is unrecognised."""
        return Role.from_value(self.role)

    @property
    def is_locked(self) -> bool:
        """Whether a lockout window is currently in force."""
        locked_until = as_utc(self.locked_until)
        if locked_until is None:
            return False
        return locked_until > utcnow()

    def __repr__(self) -> str:
        # Never the email: a repr ends up in logs and exception messages.
        return f"User(id={self.id!s}, role={self.role!r}, active={self.is_active})"


class RefreshToken(Base):
    """
    One issued refresh token, stored as a digest.

    Rows are kept after revocation rather than deleted, so "this token was
    revoked at 14:02" remains answerable. Pruning is a separate maintenance
    concern.
    """

    __tablename__ = REFRESH_TOKENS_TABLE

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: The token's `jti` claim, for correlating with a decoded token.
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    #: SHA-256 of the token string. The token itself is never stored.
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey(f"{USERS_TABLE}.id", ondelete="CASCADE"), nullable=False, index=True
    )

    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP_TYPE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP_TYPE, nullable=False, default=utcnow
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP_TYPE, nullable=True)
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    #: Set when this token is exchanged, pointing at its replacement. Makes a
    #: rotation chain reconstructable, which is how reuse of an already-rotated
    #: token is detected.
    replaced_by_jti: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    __table_args__ = (
        # "every live session for this user" -- the query logout-everywhere and
        # deactivation both run.
        Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )

    @property
    def is_revoked(self) -> bool:
        """Whether this token has been revoked."""
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        """Whether this token is past its expiry."""
        expires_at = as_utc(self.expires_at)
        return expires_at is None or expires_at <= utcnow()

    @property
    def is_usable(self) -> bool:
        """Whether this token may still be exchanged."""
        return not self.is_revoked and not self.is_expired

    def __repr__(self) -> str:
        return (
            f"RefreshToken(jti={self.jti!r}, user_id={self.user_id!s}, "
            f"revoked={self.is_revoked})"
        )


__all__ = [
    "REFRESH_TOKENS_TABLE",
    "USERS_TABLE",
    "RefreshToken",
    "User",
]
