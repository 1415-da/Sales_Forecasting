"""
Administrative user management.

Two safety rules, both enforced here rather than left to the caller:

1. **An administrator cannot lock everyone out.** Demoting or deactivating the
   last active ADMIN is refused. Without this, one mis-click leaves a
   deployment with no way back in except direct database access.
2. **An administrator cannot change their own role or status.** Self-demotion
   is almost always a mistake, and self-deactivation is always one. Another
   admin can do it deliberately.

Every mutation writes an audit record, and the record names the previous value
as well as the new one: "role changed to ANALYST" is far less useful after the
fact than "role changed from ADMIN to ANALYST".
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.core.errors import (
    InvalidStateTransitionError,
    PermissionDeniedError,
    UserNotFoundError,
)
from api.core.rbac import Role
from api.core.security import (
    generate_password,
    hash_password,
    validate_password_strength,
)
from api.db.models.audit import AuditAction, ResourceType
from api.db.models.user import User
from api.db.repositories.user_repository import (
    RefreshTokenRepository,
    UserRepository,
    normalise_email,
)
from api.services.audit_service import ActorContext, AuditService
from api.services.auth_service import REVOKE_DEACTIVATED, REVOKE_ROLE_CHANGED

logger = logging.getLogger(__name__)


class UserService:
    """Administrative operations over the user table."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.tokens = RefreshTokenRepository(session)
        self.audit = AuditService(session)

    # -- reads -------------------------------------------------------------

    def list_users(
        self,
        *,
        role: Optional[Role] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[User], int]:
        """A page of users, newest first, with the unpaginated total."""
        return self.users.list_users(
            role=role, is_active=is_active, search=search, limit=limit, offset=offset
        )

    def require_user(self, user_id: str) -> User:
        """One user by id, or a 404."""
        try:
            identifier = uuid.UUID(str(user_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise UserNotFoundError(field="user_id") from exc
        user = self.users.get(identifier)
        if user is None:
            raise UserNotFoundError(field="user_id")
        return user

    # -- writes ------------------------------------------------------------

    def create_user(
        self,
        *,
        email: str,
        full_name: str,
        role: Role,
        actor: ActorContext,
        password: Optional[str] = None,
        is_active: bool = True,
    ) -> Tuple[User, Optional[str]]:
        """
        Create an account, returning ``(user, generated_password_or_None)``.

        A generated password is returned exactly once and never stored in
        readable form. Either way the account must change it at first sign-in:
        a credential that an administrator has seen should not stay in use.
        """
        canonical = normalise_email(email)
        generated: Optional[str] = None

        if password:
            validate_password_strength(password, email=canonical)
            chosen = password
        else:
            chosen = generate_password()
            generated = chosen

        user = self.users.create(
            email=canonical,
            full_name=full_name,
            hashed_password=hash_password(chosen),
            role=role,
            is_active=is_active,
            must_change_password=True,
            created_by_id=actor.user_id,
        )

        self.audit.record(
            action=AuditAction.USER_CREATED,
            resource_type=ResourceType.USER,
            resource_id=str(user.id),
            actor=actor,
            metadata={
                "email": canonical,
                "role": role.value,
                "is_active": is_active,
                "password_generated": generated is not None,
            },
        )
        return user, generated

    def update_user(
        self,
        *,
        user: User,
        actor: ActorContext,
        full_name: Optional[str] = None,
        role: Optional[Role] = None,
        is_active: Optional[bool] = None,
    ) -> User:
        """
        Apply an edit, auditing each field that actually changed.

        Role and status changes go through the guard rails below; a name change
        is unguarded because it cannot lock anyone out.
        """
        changes: dict = {}
        audited: dict = {}

        if full_name is not None and full_name != user.full_name:
            changes["full_name"] = full_name
            audited["full_name"] = {"from": user.full_name, "to": full_name}

        role_changed = role is not None and role.value != user.role
        if role_changed:
            self._guard_role_change(user=user, new_role=role, actor=actor)
            changes["role"] = role.value
            audited["role"] = {"from": user.role, "to": role.value}

        status_changed = is_active is not None and bool(is_active) != bool(user.is_active)
        if status_changed:
            if is_active:
                self._guard_self_change(user=user, actor=actor, what="reactivate")
            else:
                self._guard_deactivation(user=user, actor=actor)
            changes["is_active"] = bool(is_active)
            audited["is_active"] = {"from": user.is_active, "to": bool(is_active)}

        if not changes:
            return user

        self.users.update(user, **changes)

        # A role or status change must take effect on existing sessions, not
        # only on the next sign-in. Without this, a demoted user keeps their old
        # permissions until their access token expires.
        if role_changed or (status_changed and not is_active):
            reason = REVOKE_DEACTIVATED if status_changed and not is_active else REVOKE_ROLE_CHANGED
            revoked = self.tokens.revoke_all_for_user(user.id, reason=reason)
            audited["sessions_revoked"] = revoked

        if role_changed:
            self.audit.record(
                action=AuditAction.USER_ROLE_CHANGED,
                resource_type=ResourceType.USER,
                resource_id=str(user.id),
                actor=actor,
                metadata={"email": user.email, **audited},
            )
        elif status_changed:
            self.audit.record(
                action=(
                    AuditAction.USER_ACTIVATED
                    if is_active
                    else AuditAction.USER_DEACTIVATED
                ),
                resource_type=ResourceType.USER,
                resource_id=str(user.id),
                actor=actor,
                metadata={"email": user.email, **audited},
            )
        else:
            self.audit.record(
                action=AuditAction.USER_UPDATED,
                resource_type=ResourceType.USER,
                resource_id=str(user.id),
                actor=actor,
                metadata={"email": user.email, **audited},
            )
        return user

    def set_active(
        self, *, user: User, active: bool, actor: ActorContext
    ) -> User:
        """Deactivate or reactivate an account."""
        return self.update_user(user=user, actor=actor, is_active=active)

    def reset_password(
        self, *, user: User, actor: ActorContext
    ) -> Tuple[User, str, int]:
        """
        Reset an account's password to a generated one.

        Returns ``(user, temporary_password, sessions_revoked)``. The plaintext
        is returned once and never persisted; every session is revoked, because
        a reset that leaves old sessions alive has not actually secured
        anything.
        """
        temporary = generate_password()
        self.users.update(
            user,
            hashed_password=hash_password(temporary),
            must_change_password=True,
            failed_login_count=0,
            locked_until=None,
        )
        revoked = self.tokens.revoke_all_for_user(user.id, reason="password_reset")

        self.audit.record(
            action=AuditAction.USER_PASSWORD_RESET,
            resource_type=ResourceType.USER,
            resource_id=str(user.id),
            actor=actor,
            metadata={"email": user.email, "sessions_revoked": revoked},
        )
        return user, temporary, revoked

    # -- guard rails -------------------------------------------------------

    def _active_admin_count(self, *, excluding: Optional[uuid.UUID] = None) -> int:
        """How many active ADMIN accounts exist, optionally excluding one."""
        statement = select(func.count(User.id)).where(
            User.role == Role.ADMIN.value, User.is_active.is_(True)
        )
        if excluding is not None:
            statement = statement.where(User.id != excluding)
        return int(self.session.execute(statement).scalar() or 0)

    def _guard_self_change(
        self, *, user: User, actor: ActorContext, what: str
    ) -> None:
        """Refuse an administrator changing their own role or status."""
        if actor.user_id is not None and actor.user_id == user.id:
            raise PermissionDeniedError(
                f"You cannot {what} your own account. Ask another administrator.",
                field="user_id",
            )

    def _guard_role_change(
        self, *, user: User, new_role: Role, actor: ActorContext
    ) -> None:
        """Refuse a role change that would remove the last administrator."""
        self._guard_self_change(user=user, actor=actor, what="change the role of")
        if (
            user.role == Role.ADMIN.value
            and new_role is not Role.ADMIN
            and self._active_admin_count(excluding=user.id) == 0
        ):
            raise InvalidStateTransitionError(
                "This is the only active administrator. Promote another account "
                "to ADMIN before changing this one.",
                field="role",
            )

    def _guard_deactivation(self, *, user: User, actor: ActorContext) -> None:
        """Refuse a deactivation that would remove the last administrator."""
        self._guard_self_change(user=user, actor=actor, what="deactivate")
        if (
            user.role == Role.ADMIN.value
            and self._active_admin_count(excluding=user.id) == 0
        ):
            raise InvalidStateTransitionError(
                "This is the only active administrator. Promote another account "
                "to ADMIN before deactivating this one.",
                field="is_active",
            )


__all__ = ["UserService"]
