"""
Audit log writing and reading.

Two rules shape this module:

1. **A failed audit write must not undo the action it describes.** A model that
   was promoted stays promoted even if the log row could not be written; the
   failure is logged at ERROR so the gap is visible rather than silent.
2. **Role scoping is applied in SQL, not after the fetch.** An ML_ENGINEER is
   given an explicit allowlist of action names, so there is no point at which
   rows they may not see exist in memory.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from api.core.rbac import Permission, Role, has_permission
from api.db.base import utcnow
from api.db.models.audit import (
    MODEL_SCOPED_ACTIONS,
    AuditAction,
    AuditLog,
    ResourceType,
)
from api.db.repositories.audit_repository import AuditRepository

logger = logging.getLogger(__name__)

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_DENIED = "denied"


@dataclass(frozen=True)
class ActorContext:
    """
    Who performed an action, and from where.

    Built once per request and threaded through, so a service never has to
    reach for the request object to attribute what it did.
    """

    user_id: Optional[uuid.UUID] = None
    email: Optional[str] = None
    role: Optional[Role] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    request_id: Optional[str] = None

    @property
    def role_value(self) -> Optional[str]:
        """The role name, if a role is set."""
        return self.role.value if self.role else None

    @classmethod
    def anonymous(
        cls,
        *,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> "ActorContext":
        """Context for an unauthenticated action, such as a failed login."""
        return cls(
            ip_address=ip_address, user_agent=user_agent, request_id=request_id
        )


class AuditService:
    """Records and queries audit events."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = AuditRepository(session)

    def record(
        self,
        *,
        action: AuditAction,
        resource_type: ResourceType,
        actor: ActorContext,
        resource_id: Optional[str] = None,
        outcome: str = OUTCOME_SUCCESS,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[AuditLog]:
        """
        Write one audit record, returning it, or ``None`` if the write failed.

        Never raises. An audit failure is a monitoring problem, not a reason to
        fail the user's request after the work has already been done.
        """
        try:
            return self.repository.append(
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                user_id=actor.user_id,
                user_email=actor.email,
                user_role=actor.role_value,
                outcome=outcome,
                metadata=metadata,
                ip_address=actor.ip_address,
                user_agent=actor.user_agent,
                request_id=actor.request_id,
            )
        except Exception as exc:  # noqa: BLE001 - must never break the caller
            logger.error(
                "Failed to write audit record action=%s resource=%s:%s: %s",
                action.value,
                resource_type.value,
                resource_id,
                exc,
            )
            return None

    def record_denial(
        self,
        *,
        actor: ActorContext,
        permission: Permission,
        resource_type: ResourceType = ResourceType.SYSTEM,
        resource_id: Optional[str] = None,
    ) -> None:
        """
        Record a rejected privileged attempt, in its own transaction.

        A separate session is essential, not incidental. The caller raises
        ``PermissionDeniedError`` immediately afterwards; the request session
        then rolls back, and a row written into *that* session would be
        discarded along with it. Denials were silently absent from the audit
        log for exactly that reason.

        A pattern of denials is a signal worth keeping: either someone is
        probing, or a role is mis-assigned and a colleague is quietly stuck.
        """
        metadata = {
            "required_permission": permission.value,
            "actor_role": actor.role_value,
        }
        try:
            from api.db.session import session_scope

            with session_scope() as session:
                AuditRepository(session).append(
                    action=AuditAction.PERMISSION_DENIED,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    user_id=actor.user_id,
                    user_email=actor.email,
                    user_role=actor.role_value,
                    outcome=OUTCOME_DENIED,
                    metadata=metadata,
                    ip_address=actor.ip_address,
                    user_agent=actor.user_agent,
                    request_id=actor.request_id,
                )
        except Exception as exc:  # noqa: BLE001 - must never break the 403
            logger.error(
                "Failed to record a permission denial for %s on %s: %s",
                actor.email,
                permission.value,
                exc,
            )

    def visible_actions(self, role: Optional[Role]) -> Optional[Sequence[str]]:
        """
        The action names a role may read, or ``None`` for "all of them".

        ADMIN sees everything. ML_ENGINEER sees model and monitoring activity
        but not other people's account history, which is an administrator's
        concern. Anyone else sees nothing and should not have reached here.
        """
        if has_permission(role, Permission.VIEW_ALL_AUDIT_LOGS):
            return None
        if has_permission(role, Permission.VIEW_AUDIT_LOGS):
            return sorted(action.value for action in MODEL_SCOPED_ACTIONS)
        return []

    def search(
        self,
        *,
        role: Optional[Role],
        actions: Optional[Sequence[str]] = None,
        user_id: Optional[uuid.UUID] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        outcome: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[AuditLog], int]:
        """
        A page of audit records the role is allowed to see.

        A caller-supplied ``actions`` filter is intersected with the role's
        allowlist rather than replacing it, so a crafted query parameter cannot
        widen what an ML_ENGINEER can read.
        """
        allowed = self.visible_actions(role)
        if allowed is None:
            effective = list(actions) if actions else None
        else:
            permitted = set(allowed)
            effective = (
                sorted(permitted.intersection(actions)) if actions else sorted(permitted)
            )

        return self.repository.search(
            actions=effective,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )

    def available_action_counts(
        self, *, role: Optional[Role], days: int = 90
    ) -> Dict[str, int]:
        """Counts per action over a recent window, scoped to the role."""
        counts = self.repository.action_counts(
            start=utcnow() - timedelta(days=max(1, days))
        )
        allowed = self.visible_actions(role)
        if allowed is None:
            return counts
        permitted = set(allowed)
        return {name: value for name, value in counts.items() if name in permitted}


__all__ = [
    "OUTCOME_DENIED",
    "OUTCOME_FAILURE",
    "OUTCOME_SUCCESS",
    "ActorContext",
    "AuditService",
]
