"""
Repository for scenario audit events.

The audit table is defined with ``ScenarioAuditService``; this repository adds
the query surface the operations and reliability views need -- recent failures
and duration statistics -- without widening that service's responsibilities.

Audit rows are append-only here: there is no update or delete method, because
an audit trail that can be rewritten is not an audit trail.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from api.core.errors import sanitize_text
from api.db.models.scenario import ScenarioAuditEvent
from api.db.repositories.scenario_repository import BaseRepository
from api.services.scenario_audit_service import ScenarioAuditStatus

logger = logging.getLogger(__name__)

ERROR_MESSAGE_MAX_LENGTH = 4000

#: Statuses that represent a failed attempt.
FAILURE_STATUSES = (
    ScenarioAuditStatus.VALIDATION_FAILED.value,
    ScenarioAuditStatus.CAPABILITY_CHECK_FAILED.value,
    ScenarioAuditStatus.BASELINE_FAILED.value,
    ScenarioAuditStatus.SCENARIO_FAILED.value,
    ScenarioAuditStatus.SCENARIO_CANCELLED.value,
)


class ScenarioDurationStats(BaseModel):
    """Duration statistics over completed scenario attempts."""

    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(default=0, ge=0)
    completed_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    avg_duration_ms: Optional[float] = None
    min_duration_ms: Optional[int] = None
    max_duration_ms: Optional[int] = None

    @property
    def success_rate(self) -> Optional[float]:
        """Share of attempts that completed, or None when there were none."""
        total = self.completed_count + self.failure_count
        if total == 0:
            return None
        return self.completed_count / total


class ScenarioAuditRepository(BaseRepository):
    """Append-only access to the scenario audit trail."""

    def create_audit_event(
        self,
        *,
        request_id: str,
        store_id: str,
        model_version: str,
        scenario_type: str,
        status: ScenarioAuditStatus,
        request_payload: Dict[str, Any],
        scenario_id: Optional[str] = None,
        baseline_forecast_id: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        response_summary: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        request_hash: Optional[str] = None,
    ) -> ScenarioAuditEvent:
        """Append one audit event."""
        event = ScenarioAuditEvent(
            id=uuid.uuid4(),
            request_id=request_id,
            scenario_id=scenario_id,
            baseline_forecast_id=baseline_forecast_id,
            store_id=store_id,
            model_version=model_version,
            scenario_type=scenario_type,
            status=status.value,
            error_code=error_code,
            error_message=(
                sanitize_text(error_message)[:ERROR_MESSAGE_MAX_LENGTH]
                if error_message
                else None
            ),
            request_payload=request_payload,
            response_summary=response_summary,
            duration_ms=duration_ms,
            created_at=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

        try:
            with self.session_factory() as session:
                session.add(event)
                session.commit()
                session.refresh(event)
                return event
        except SQLAlchemyError as exc:
            raise self._unavailable("insert audit event", exc) from exc

    def get_audit_events_by_scenario(self, scenario_id: str) -> List[ScenarioAuditEvent]:
        """Every event for a scenario, oldest first."""
        try:
            with self.session_factory() as session:
                stmt = (
                    select(ScenarioAuditEvent)
                    .where(ScenarioAuditEvent.scenario_id == scenario_id)
                    .order_by(ScenarioAuditEvent.created_at)
                )
                return list(session.scalars(stmt).all())
        except SQLAlchemyError as exc:
            raise self._unavailable("read audit events", exc) from exc

    def get_recent_failures(
        self,
        limit: int = 20,
        since: Optional[datetime] = None,
        store_id: Optional[str] = None,
    ) -> List[ScenarioAuditEvent]:
        """Most recent failed attempts, newest first."""
        try:
            with self.session_factory() as session:
                stmt = select(ScenarioAuditEvent).where(
                    ScenarioAuditEvent.status.in_(FAILURE_STATUSES)
                )
                if since is not None:
                    stmt = stmt.where(ScenarioAuditEvent.created_at >= since)
                if store_id:
                    stmt = stmt.where(ScenarioAuditEvent.store_id == store_id)
                stmt = stmt.order_by(ScenarioAuditEvent.created_at.desc()).limit(limit)
                return list(session.scalars(stmt).all())
        except SQLAlchemyError as exc:
            raise self._unavailable("read recent failures", exc) from exc

    def get_scenario_duration_stats(
        self,
        store_id: Optional[str] = None,
        since: Optional[datetime] = None,
        window: Optional[timedelta] = None,
    ) -> ScenarioDurationStats:
        """
        Duration and outcome statistics over recorded attempts.

        Only rows that actually carry a ``duration_ms`` contribute to the
        averages, so a failure recorded before timing started cannot drag the
        mean toward zero. Failures are still counted in ``failure_count``.
        """
        if since is None and window is not None:
            since = datetime.now(timezone.utc) - window

        try:
            with self.session_factory() as session:
                timed = select(
                    func.count(ScenarioAuditEvent.id),
                    func.avg(ScenarioAuditEvent.duration_ms),
                    func.min(ScenarioAuditEvent.duration_ms),
                    func.max(ScenarioAuditEvent.duration_ms),
                ).where(ScenarioAuditEvent.duration_ms.is_not(None))
                timed = self._scope(timed, store_id, since)
                sample_count, avg_ms, min_ms, max_ms = session.execute(timed).one()

                completed = select(func.count(ScenarioAuditEvent.id)).where(
                    ScenarioAuditEvent.status
                    == ScenarioAuditStatus.SCENARIO_COMPLETED.value
                )
                completed_count = session.execute(
                    self._scope(completed, store_id, since)
                ).scalar_one()

                failures = select(func.count(ScenarioAuditEvent.id)).where(
                    ScenarioAuditEvent.status.in_(FAILURE_STATUSES)
                )
                failure_count = session.execute(
                    self._scope(failures, store_id, since)
                ).scalar_one()

                return ScenarioDurationStats(
                    sample_count=int(sample_count or 0),
                    completed_count=int(completed_count or 0),
                    failure_count=int(failure_count or 0),
                    avg_duration_ms=float(avg_ms) if avg_ms is not None else None,
                    min_duration_ms=int(min_ms) if min_ms is not None else None,
                    max_duration_ms=int(max_ms) if max_ms is not None else None,
                )
        except SQLAlchemyError as exc:
            raise self._unavailable("read duration stats", exc) from exc

    @staticmethod
    def _scope(stmt: Any, store_id: Optional[str], since: Optional[datetime]) -> Any:
        """Apply the shared store/time filters to an aggregate query."""
        if store_id:
            stmt = stmt.where(ScenarioAuditEvent.store_id == store_id)
        if since is not None:
            stmt = stmt.where(ScenarioAuditEvent.created_at >= since)
        return stmt
