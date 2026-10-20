"""
Audit trail for scenario attempts.

Every scenario request is recorded in ``scenario_audit_events``, from the moment
it is received through to completion, failure, or rollback. Audit rows are
append-and-update only: nothing in this service deletes history.

The table also backs idempotency. Two nullable columns (``idempotency_key``,
``request_hash``) let a retried request be matched to its original attempt, and
``response_summary`` holds the replayable result. A dedicated idempotency table
would be tidier, but the specified schema allows exactly one table and the two
concerns share the same grain: one row per request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, DateTime, Integer, String, Text, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from api.core.errors import (
    DatabaseUnavailableError,
    IdempotencyKeyConflictError,
    sanitize_text,
)
from api.services.forecast_checkpoint_service import Base, resolve_database_url

logger = logging.getLogger(__name__)

TABLE_NAME = "scenario_audit_events"
ERROR_MESSAGE_MAX_LENGTH = 4000

# JSONB on PostgreSQL, plain JSON elsewhere, so the same model works in tests.
JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class ScenarioAuditStatus(str, Enum):
    """Lifecycle of one scenario attempt."""

    RECEIVED = "received"
    VALIDATION_FAILED = "validation_failed"
    CAPABILITY_CHECK_FAILED = "capability_check_failed"
    BASELINE_CREATED = "baseline_created"
    SCENARIO_COMPLETED = "scenario_completed"
    SCENARIO_FAILED = "scenario_failed"
    ROLLED_BACK = "rolled_back"
    # Additive: the rollback contract names both of these as audit outcomes.
    BASELINE_FAILED = "baseline_failed"
    SCENARIO_CANCELLED = "scenario_cancelled"


TERMINAL_STATUSES = frozenset(
    {
        ScenarioAuditStatus.VALIDATION_FAILED,
        ScenarioAuditStatus.CAPABILITY_CHECK_FAILED,
        ScenarioAuditStatus.SCENARIO_COMPLETED,
        ScenarioAuditStatus.SCENARIO_FAILED,
        ScenarioAuditStatus.BASELINE_FAILED,
        ScenarioAuditStatus.SCENARIO_CANCELLED,
        ScenarioAuditStatus.ROLLED_BACK,
    }
)


class ScenarioAuditEvent(Base):
    """One audit row per scenario attempt."""

    __tablename__ = TABLE_NAME

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scenario_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    baseline_forecast_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    store_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    request_payload: Mapped[Dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    response_summary: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSON_TYPE, nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    # Additive, both nullable: idempotent replay support.
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    request_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return (
            f"ScenarioAuditEvent(request_id={self.request_id!r}, "
            f"status={self.status!r}, scenario_id={self.scenario_id!r})"
        )


def compute_request_hash(payload: Any) -> str:
    """Stable SHA-256 over a request payload, independent of key order."""
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ScenarioAuditService:
    """
    Records and queries scenario audit events.

    Audit writes must never mask the failure they describe, so
    ``record_safely`` swallows database problems after logging them. Direct
    calls raise ``DatabaseUnavailableError`` so a caller that depends on the
    write can react.
    """

    def __init__(
        self,
        engine: Optional[Any] = None,
        database_url: Optional[str] = None,
        create_tables: bool = False,
    ) -> None:
        self._engine = engine
        self._database_url = database_url
        self._session_factory: Optional[sessionmaker] = None
        if create_tables:
            self.create_all()

    @property
    def engine(self) -> Any:
        """The SQLAlchemy engine, created on first use."""
        if self._engine is None:
            from sqlalchemy import create_engine

            url = resolve_database_url(self._database_url)
            try:
                self._engine = create_engine(url, pool_pre_ping=True, future=True)
            except SQLAlchemyError as exc:
                raise self._unavailable("create database engine", exc) from exc
        return self._engine

    @property
    def session_factory(self) -> sessionmaker:
        """Session factory bound to the engine."""
        if self._session_factory is None:
            self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        return self._session_factory

    def create_all(self) -> None:
        """Create the audit table when it does not exist."""
        try:
            Base.metadata.create_all(self.engine)
        except SQLAlchemyError as exc:
            raise self._unavailable("create audit table", exc) from exc

    @staticmethod
    def _unavailable(action: str, exc: Exception) -> DatabaseUnavailableError:
        """Wrap a driver failure, keeping the raw text out of the response."""
        logger.error("Audit database error while trying to %s: %s", action, exc)
        return DatabaseUnavailableError(
            details={"action": action, "error": sanitize_text(str(exc))}
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record(
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
        """Insert one audit event."""
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
            error_message=(error_message or None) and error_message[:ERROR_MESSAGE_MAX_LENGTH],
            request_payload=request_payload,
            response_summary=response_summary,
            duration_ms=duration_ms,
            created_at=datetime.now(timezone.utc),
            idempotency_key=idempotency_key,
            request_hash=request_hash
            or (compute_request_hash(request_payload) if idempotency_key else None),
        )

        try:
            with self.session_factory() as session:
                session.add(event)
                session.commit()
                session.refresh(event)
                return event
        except SQLAlchemyError as exc:
            raise self._unavailable("insert audit event", exc) from exc

    def record_safely(self, **kwargs: Any) -> Optional[ScenarioAuditEvent]:
        """
        Insert an audit event, never raising.

        Used on failure paths: losing an audit row is bad, but masking the
        original error with a database error is worse.
        """
        try:
            return self.record(**kwargs)
        except DatabaseUnavailableError as exc:
            logger.error(
                "Could not write audit event for request %s: %s",
                kwargs.get("request_id"),
                exc.message,
            )
            return None

    def update_status(
        self,
        request_id: str,
        status: ScenarioAuditStatus,
        scenario_id: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        response_summary: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> Optional[ScenarioAuditEvent]:
        """
        Advance the latest event for a request to a new status.

        History is preserved: this updates the most recent row's status rather
        than deleting or rewriting earlier rows.
        """
        try:
            with self.session_factory() as session:
                event = self._latest_for_request(session, request_id)
                if event is None:
                    return None
                event.status = status.value
                if scenario_id is not None:
                    event.scenario_id = scenario_id
                if error_code is not None:
                    event.error_code = error_code
                if error_message is not None:
                    event.error_message = error_message[:ERROR_MESSAGE_MAX_LENGTH]
                if response_summary is not None:
                    event.response_summary = response_summary
                if duration_ms is not None:
                    event.duration_ms = duration_ms
                session.commit()
                session.refresh(event)
                return event
        except SQLAlchemyError as exc:
            raise self._unavailable(f"update audit event to {status.value}", exc) from exc

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_by_request_id(self, request_id: str) -> List[ScenarioAuditEvent]:
        """Every event recorded for one request, oldest first."""
        try:
            with self.session_factory() as session:
                stmt = (
                    select(ScenarioAuditEvent)
                    .where(ScenarioAuditEvent.request_id == request_id)
                    .order_by(ScenarioAuditEvent.created_at)
                )
                return list(session.scalars(stmt).all())
        except SQLAlchemyError as exc:
            raise self._unavailable("read audit events", exc) from exc

    def get_by_scenario_id(self, scenario_id: str) -> List[ScenarioAuditEvent]:
        """Every event recorded for one scenario, oldest first."""
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

    def list_for_store(self, store_id: str, limit: int = 50) -> List[ScenarioAuditEvent]:
        """Most recent events for a store, newest first."""
        try:
            with self.session_factory() as session:
                stmt = (
                    select(ScenarioAuditEvent)
                    .where(ScenarioAuditEvent.store_id == store_id)
                    .order_by(ScenarioAuditEvent.created_at.desc())
                    .limit(limit)
                )
                return list(session.scalars(stmt).all())
        except SQLAlchemyError as exc:
            raise self._unavailable("list audit events", exc) from exc

    @staticmethod
    def _latest_for_request(
        session: Session, request_id: str
    ) -> Optional[ScenarioAuditEvent]:
        """Most recent event for a request."""
        stmt = (
            select(ScenarioAuditEvent)
            .where(ScenarioAuditEvent.request_id == request_id)
            .order_by(ScenarioAuditEvent.created_at.desc())
        )
        return session.scalars(stmt).first()

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def find_completed_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[ScenarioAuditEvent]:
        """The completed attempt for a key, if one exists."""
        try:
            with self.session_factory() as session:
                stmt = (
                    select(ScenarioAuditEvent)
                    .where(
                        ScenarioAuditEvent.idempotency_key == idempotency_key,
                        ScenarioAuditEvent.status
                        == ScenarioAuditStatus.SCENARIO_COMPLETED.value,
                    )
                    .order_by(ScenarioAuditEvent.created_at.desc())
                )
                return session.scalars(stmt).first()
        except SQLAlchemyError as exc:
            raise self._unavailable("read idempotency record", exc) from exc

    def find_any_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[ScenarioAuditEvent]:
        """The most recent attempt for a key, whatever its status."""
        try:
            with self.session_factory() as session:
                stmt = (
                    select(ScenarioAuditEvent)
                    .where(ScenarioAuditEvent.idempotency_key == idempotency_key)
                    .order_by(ScenarioAuditEvent.created_at.desc())
                )
                return session.scalars(stmt).first()
        except SQLAlchemyError as exc:
            raise self._unavailable("read idempotency record", exc) from exc

    def resolve_idempotent_request(
        self, idempotency_key: Optional[str], request_payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Replay a previous result, or raise on a conflicting reuse.

        Returns the stored ``response_summary`` when the same key is reused with
        an identical payload, ``None`` when the key is new (or its earlier
        attempt never completed), and raises ``IdempotencyKeyConflictError``
        when the key is reused with a different payload.
        """
        if not idempotency_key:
            return None

        request_hash = compute_request_hash(request_payload)
        previous = self.find_any_by_idempotency_key(idempotency_key)
        if previous is None:
            return None

        if previous.request_hash and previous.request_hash != request_hash:
            raise IdempotencyKeyConflictError(
                field="Idempotency-Key",
                details={
                    "idempotency_key": idempotency_key,
                    "original_request_id": previous.request_id,
                    "original_status": previous.status,
                },
            )

        if previous.status == ScenarioAuditStatus.SCENARIO_COMPLETED.value:
            return previous.response_summary or {}

        # The key matches a failed or in-flight attempt: let the caller retry.
        return None
