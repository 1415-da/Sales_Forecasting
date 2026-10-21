"""
Audit log reads.

Scoped by role inside ``AuditService``: ADMIN reads everything, ML_ENGINEER
reads model and monitoring activity. A caller-supplied ``action`` filter is
intersected with their allowlist rather than replacing it, so a crafted query
parameter cannot widen what is visible.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.core.rbac import Permission, has_permission
from api.db.models.user import User
from api.db.session import get_session
from api.dependencies.auth import require_permission
from api.schemas.admin import AuditLogListResponse, AuditLogRow
from api.services.audit_service import AuditService

logger = logging.getLogger(__name__)

TAG = "Audit"

router = APIRouter(prefix="/v1/admin", tags=[TAG])


@router.get(
    "/audit-logs",
    response_model=AuditLogListResponse,
    summary="Read the audit trail",
    responses={403: {"description": "Requires audit-log access."}},
)
def audit_logs(
    action: Optional[List[str]] = Query(default=None, description="Filter by action name."),
    user_id: Optional[str] = Query(default=None),
    resource_type: Optional[str] = Query(default=None),
    resource_id: Optional[str] = Query(default=None),
    outcome: Optional[str] = Query(default=None, description="success, failure, denied"),
    start: Optional[datetime] = Query(default=None),
    end: Optional[datetime] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    viewer: User = Depends(require_permission(Permission.VIEW_AUDIT_LOGS)),
    session: Session = Depends(get_session),
) -> AuditLogListResponse:
    """
    A page of audit records, newest first.

    An ML_ENGINEER sees model, scenario, forecast, and alert activity. Only an
    ADMIN sees account activity such as logins and role changes, because that
    is personnel data rather than model operations.

    An unparseable ``user_id`` is treated as no filter rather than an error: a
    stale link should not return a 422 for a page that is otherwise fine.
    """
    service = AuditService(session)
    role = viewer.role_enum

    subject: Optional[uuid.UUID] = None
    if user_id:
        try:
            subject = uuid.UUID(user_id)
        except (ValueError, TypeError):
            logger.info("Ignoring an unparseable audit user_id filter")

    entries, total = service.search(
        role=role,
        actions=action,
        user_id=subject,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        start=start,
        end=end,
        limit=limit,
        offset=offset,
    )

    return AuditLogListResponse(
        entries=[AuditLogRow.from_row(entry) for entry in entries],
        total=total,
        limit=limit,
        offset=offset,
        available_actions=service.available_action_counts(role=role),
        unrestricted=has_permission(role, Permission.VIEW_ALL_AUDIT_LOGS),
    )


__all__ = ["TAG", "router"]
