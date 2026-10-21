"""
Audit log persistence.

Append and query only: there is no update or delete method, because an audit
trail the application can edit is not evidence of anything.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.core.errors import DatabaseUnavailableError, sanitize_details
from api.db.models.audit import AuditAction, AuditLog, ResourceType

logger = logging.getLogger(__name__)

#: Audit metadata is written verbatim into a JSON column, so it is capped:
#: an unbounded payload would let one request bloat the table.
MAX_METADATA_BYTES = 8_000
MAX_USER_AGENT_LENGTH = 512


class AuditRepository:
    """Reads and appends ``audit_logs``."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        *,
        action: AuditAction,
        resource_type: ResourceType,
        resource_id: Optional[str] = None,
        user_id: Optional[uuid.UUID] = None,
        user_email: Optional[str] = None,
        user_role: Optional[str] = None,
        outcome: str = "success",
        metadata: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> AuditLog:
        """
        Write one audit record.

        ``metadata`` is passed through the same redaction the error envelope
        uses, so a token or a connection string cannot be persisted even if a
        caller puts one in the dict by mistake.
        """
        entry = AuditLog(
            action=action.value,
            resource_type=resource_type.value,
            resource_id=(str(resource_id)[:128] if resource_id else None),
            user_id=user_id,
            user_email=(user_email.lower() if user_email else None),
            user_role=user_role,
            outcome=outcome,
            event_metadata=_safe_metadata(metadata),
            ip_address=(ip_address[:64] if ip_address else None),
            user_agent=(user_agent[:MAX_USER_AGENT_LENGTH] if user_agent else None),
            request_id=(request_id[:128] if request_id else None),
        )
        self.session.add(entry)
        try:
            self.session.flush()
        except SQLAlchemyError as exc:
            logger.error("Could not write audit record %s: %s", action.value, exc)
            raise DatabaseUnavailableError(
                "The audit log is currently unavailable.",
                details={"operation": "append audit record"},
            ) from exc
        return entry

    def search(
        self,
        *,
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
        A page of audit records, newest first, with the unpaginated total.

        ``actions`` is how role scoping is applied: an ML_ENGINEER is handed the
        model- and monitoring-related action names, so the filter is enforced in
        SQL rather than by discarding rows after the fact.
        """
        filters = []
        if actions is not None:
            # An empty allowlist means "nothing", not "everything" -- the
            # difference between a correctly scoped reader and a data leak.
            filters.append(AuditLog.action.in_(list(actions) or ["__none__"]))
        if user_id is not None:
            filters.append(AuditLog.user_id == user_id)
        if resource_type:
            filters.append(AuditLog.resource_type == resource_type)
        if resource_id:
            filters.append(AuditLog.resource_id == resource_id)
        if outcome:
            filters.append(AuditLog.outcome == outcome)
        if start is not None:
            filters.append(AuditLog.created_at >= start)
        if end is not None:
            filters.append(AuditLog.created_at <= end)

        page = (
            select(AuditLog)
            .where(*filters)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
        total_statement = select(func.count(AuditLog.id)).where(*filters)

        try:
            rows = list(self.session.execute(page).scalars())
            total = int(self.session.execute(total_statement).scalar() or 0)
        except SQLAlchemyError as exc:
            logger.error("Audit search failed: %s", exc)
            raise DatabaseUnavailableError(
                "The audit log is currently unavailable.",
                details={"operation": "search audit records"},
            ) from exc
        return rows, total

    def action_counts(self, *, start: Optional[datetime] = None) -> Dict[str, int]:
        """
        How many records per action since a cutoff.

        Drives the audit page's filter chips, which would otherwise have to
        offer every action name whether or not any record exists.
        """
        statement = select(AuditLog.action, func.count(AuditLog.id)).group_by(
            AuditLog.action
        )
        if start is not None:
            statement = statement.where(AuditLog.created_at >= start)
        try:
            return {row[0]: int(row[1]) for row in self.session.execute(statement)}
        except SQLAlchemyError as exc:
            logger.warning("Audit action counts unavailable: %s", exc)
            return {}


def _safe_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Redact and size-cap an audit metadata payload."""
    if not metadata:
        return {}
    cleaned = sanitize_details(dict(metadata))
    if not isinstance(cleaned, dict):
        return {}
    encoded = repr(cleaned)
    if len(encoded) > MAX_METADATA_BYTES:
        return {
            "truncated": True,
            "reason": "metadata exceeded the size limit",
            "keys": sorted(str(key) for key in cleaned)[:50],
        }
    return cleaned


__all__ = ["MAX_METADATA_BYTES", "AuditRepository"]
