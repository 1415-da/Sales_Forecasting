"""
Failure handling and rollback for the Scenario Planner.

Scenario generation touches two durable things: the baseline checkpoint and the
audit trail. This service owns what happens to both when a step fails, and it
holds one rule above all others: **a completed baseline is never invalidated by
a scenario failure.** The baseline is independently useful, and scenarios are
derived from it, not the reverse.

Rollback is a status transition, never a delete. Audit rows and baseline
forecasts both survive it.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from api.core.errors import (
    DatabaseUnavailableError,
    ScenarioAlreadyRunningError,
    ScenarioPlannerError,
    normalize_error,
    sanitize_text,
)
from api.schemas.baseline_forecast import BaselineForecastStatus
from api.services.forecast_checkpoint_service import CheckpointError, ForecastCheckpointService
from api.services.scenario_audit_service import ScenarioAuditService, ScenarioAuditStatus

logger = logging.getLogger(__name__)


class ScenarioStage(str, Enum):
    """Where in the pipeline a failure happened."""

    VALIDATION = "validation"
    CAPABILITY_CHECK = "capability_check"
    BASELINE_CREATION = "baseline_creation"
    FEATURE_PREPARATION = "feature_preparation"
    PREDICTION = "prediction"
    PERSISTENCE = "persistence"
    CANCELLATION = "cancellation"


# Which audit status each failing stage produces.
STAGE_AUDIT_STATUS: Dict[ScenarioStage, ScenarioAuditStatus] = {
    ScenarioStage.VALIDATION: ScenarioAuditStatus.VALIDATION_FAILED,
    ScenarioStage.CAPABILITY_CHECK: ScenarioAuditStatus.CAPABILITY_CHECK_FAILED,
    ScenarioStage.BASELINE_CREATION: ScenarioAuditStatus.BASELINE_FAILED,
    ScenarioStage.FEATURE_PREPARATION: ScenarioAuditStatus.SCENARIO_FAILED,
    ScenarioStage.PREDICTION: ScenarioAuditStatus.SCENARIO_FAILED,
    ScenarioStage.PERSISTENCE: ScenarioAuditStatus.SCENARIO_FAILED,
    ScenarioStage.CANCELLATION: ScenarioAuditStatus.SCENARIO_CANCELLED,
}


class RollbackResponse(BaseModel):
    """Outcome of a rollback or failure-handling call."""

    model_config = ConfigDict(extra="forbid")

    rolled_back: bool
    scenario_id: Optional[str] = None
    request_id: Optional[str] = None
    status: ScenarioAuditStatus
    stage: Optional[ScenarioStage] = None
    reason: str
    error_code: Optional[str] = None
    baseline_forecast_id: Optional[str] = None
    baseline_preserved: bool = False
    baseline_status: Optional[BaselineForecastStatus] = None
    audit_event_recorded: bool = False
    audit_history_preserved: bool = True
    partial_results_discarded: bool = False
    locks_released: bool = False
    rolled_back_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    notes: list[str] = Field(default_factory=list)


class ScenarioLockRegistry:
    """
    In-process guard against concurrent scenarios for the same target.

    Scoped to one worker process. A multi-worker deployment needs a shared lock
    (advisory locks in PostgreSQL, or Redis); this class is the seam where that
    swaps in, and ``SCENARIO_ALREADY_RUNNING`` already exists in the taxonomy
    for it.
    """

    def __init__(self) -> None:
        self._held: Dict[str, str] = {}
        self._guard = threading.Lock()

    @staticmethod
    def build_key(store_id: str, model_version: str, scenario_type: str) -> str:
        """Lock key for one scenario target."""
        return f"{store_id}:{model_version}:{scenario_type}"

    def acquire(self, key: str, request_id: str) -> None:
        """Claim a lock, or raise if another request already holds it."""
        with self._guard:
            holder = self._held.get(key)
            if holder is not None and holder != request_id:
                raise ScenarioAlreadyRunningError(
                    field="store_id",
                    details={"lock_key": key, "held_by_request_id": holder},
                )
            self._held[key] = request_id

    def release(self, key: str, request_id: Optional[str] = None) -> bool:
        """
        Release a lock. Returns whether one was actually held.

        A release is always safe to call, including on a path where the lock was
        never acquired, so failure handlers do not need to track that state.
        """
        with self._guard:
            holder = self._held.get(key)
            if holder is None:
                return False
            if request_id is not None and holder != request_id:
                return False
            del self._held[key]
            return True

    def is_held(self, key: str) -> bool:
        """Whether a lock is currently held."""
        with self._guard:
            return key in self._held


class ScenarioRollbackService:
    """
    Applies the correct rollback for each failure stage.

    Both collaborators are injectable, so the whole matrix of failure cases is
    testable without a live database.
    """

    def __init__(
        self,
        audit_service: Optional[ScenarioAuditService] = None,
        checkpoint_service: Optional[ForecastCheckpointService] = None,
        lock_registry: Optional[ScenarioLockRegistry] = None,
    ) -> None:
        self._audit_service = audit_service
        self._checkpoint_service = checkpoint_service
        self.lock_registry = lock_registry or ScenarioLockRegistry()

    @property
    def audit_service(self) -> ScenarioAuditService:
        """Audit store, defaulting to the configured database."""
        if self._audit_service is None:
            self._audit_service = ScenarioAuditService()
        return self._audit_service

    @property
    def checkpoint_service(self) -> ForecastCheckpointService:
        """Checkpoint store, defaulting to the configured database."""
        if self._checkpoint_service is None:
            self._checkpoint_service = ForecastCheckpointService()
        return self._checkpoint_service

    # ------------------------------------------------------------------
    # Stage failure handling
    # ------------------------------------------------------------------

    def handle_failure(
        self,
        *,
        stage: ScenarioStage,
        exc: BaseException,
        request_id: str,
        store_id: str,
        model_version: str,
        scenario_type: str,
        request_payload: Dict[str, Any],
        scenario_id: Optional[str] = None,
        baseline_forecast_id: Optional[str] = None,
        lock_key: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> RollbackResponse:
        """
        Apply the rollback appropriate to ``stage`` and record it.

        Never raises: a failure handler that throws replaces a diagnosable error
        with an undiagnosable one. Problems here are logged instead.
        """
        error = normalize_error(exc)
        status = STAGE_AUDIT_STATUS[stage]
        notes: list[str] = []

        baseline_status, baseline_preserved = self._settle_baseline(
            stage=stage,
            baseline_forecast_id=baseline_forecast_id,
            error=error,
            notes=notes,
        )

        discarded = stage in (
            ScenarioStage.FEATURE_PREPARATION,
            ScenarioStage.PREDICTION,
            ScenarioStage.PERSISTENCE,
            ScenarioStage.CANCELLATION,
        )
        if stage is ScenarioStage.PERSISTENCE:
            notes.append(
                "Partial scenario results were discarded and are not returned to the caller."
            )

        audit_event = self.audit_service.record_safely(
            request_id=request_id,
            store_id=store_id,
            model_version=model_version,
            scenario_type=scenario_type,
            status=status,
            request_payload=request_payload,
            scenario_id=scenario_id,
            baseline_forecast_id=baseline_forecast_id,
            error_code=error.error_code,
            error_message=sanitize_text(error.message),
            duration_ms=duration_ms,
        )

        locks_released = self._release_lock(lock_key, request_id)

        logger.warning(
            "Scenario %s failed at stage=%s code=%s request_id=%s",
            scenario_id or "(unassigned)",
            stage.value,
            error.error_code,
            request_id,
        )

        return RollbackResponse(
            rolled_back=False,
            scenario_id=scenario_id,
            request_id=request_id,
            status=status,
            stage=stage,
            reason=sanitize_text(error.message),
            error_code=error.error_code,
            baseline_forecast_id=baseline_forecast_id,
            baseline_preserved=baseline_preserved,
            baseline_status=baseline_status,
            audit_event_recorded=audit_event is not None,
            partial_results_discarded=discarded,
            locks_released=locks_released,
            notes=notes,
        )

    def handle_baseline_failure(self, **kwargs: Any) -> RollbackResponse:
        """Baseline creation failed: no scenario record is created."""
        return self.handle_failure(stage=ScenarioStage.BASELINE_CREATION, **kwargs)

    def handle_validation_failure(self, **kwargs: Any) -> RollbackResponse:
        """The request never got as far as a baseline."""
        return self.handle_failure(stage=ScenarioStage.VALIDATION, **kwargs)

    def handle_capability_failure(self, **kwargs: Any) -> RollbackResponse:
        """The model cannot support the requested scenario."""
        return self.handle_failure(stage=ScenarioStage.CAPABILITY_CHECK, **kwargs)

    def handle_feature_preparation_failure(self, **kwargs: Any) -> RollbackResponse:
        """Override preparation failed; a completed baseline stays usable."""
        return self.handle_failure(stage=ScenarioStage.FEATURE_PREPARATION, **kwargs)

    def handle_prediction_failure(self, **kwargs: Any) -> RollbackResponse:
        """Model inference failed; a completed baseline stays usable."""
        return self.handle_failure(stage=ScenarioStage.PREDICTION, **kwargs)

    def handle_persistence_failure(self, **kwargs: Any) -> RollbackResponse:
        """A write failed after prediction; partial results are discarded."""
        return self.handle_failure(stage=ScenarioStage.PERSISTENCE, **kwargs)

    def handle_cancellation(self, **kwargs: Any) -> RollbackResponse:
        """The caller cancelled or the request timed out; locks are released."""
        return self.handle_failure(stage=ScenarioStage.CANCELLATION, **kwargs)

    # ------------------------------------------------------------------
    # Explicit rollback
    # ------------------------------------------------------------------

    def rollback_scenario(
        self,
        scenario_id: str,
        reason: str,
        request_id: Optional[str] = None,
        store_id: Optional[str] = None,
        model_version: Optional[str] = None,
        scenario_type: Optional[str] = None,
        baseline_forecast_id: Optional[str] = None,
        lock_key: Optional[str] = None,
    ) -> RollbackResponse:
        """
        Mark a scenario rolled back, preserving all history.

        Audit rows are appended and updated, never deleted, and the baseline
        forecast is left untouched: rolling back a scenario must not invalidate
        the forecast it was measured against.
        """
        existing = self._lookup_scenario_events(scenario_id)
        resolved = self._resolve_identity(
            existing,
            request_id=request_id,
            store_id=store_id,
            model_version=model_version,
            scenario_type=scenario_type,
            baseline_forecast_id=baseline_forecast_id,
        )

        audit_event = self.audit_service.record_safely(
            request_id=resolved["request_id"],
            store_id=resolved["store_id"],
            model_version=resolved["model_version"],
            scenario_type=resolved["scenario_type"],
            status=ScenarioAuditStatus.ROLLED_BACK,
            request_payload=resolved["request_payload"],
            scenario_id=scenario_id,
            baseline_forecast_id=resolved["baseline_forecast_id"],
            error_message=sanitize_text(reason),
        )

        baseline_status = self._read_baseline_status(resolved["baseline_forecast_id"])
        locks_released = self._release_lock(lock_key, resolved["request_id"])

        notes = [
            "Audit history is preserved; rollback appends an event rather than "
            "deleting any.",
            "The baseline forecast was not deleted or invalidated.",
        ]
        if not existing:
            notes.append(
                "No prior audit events were found for this scenario id; the "
                "rollback was still recorded."
            )

        logger.info(
            "Scenario %s rolled back (request_id=%s): %s",
            scenario_id,
            resolved["request_id"],
            reason,
        )

        return RollbackResponse(
            rolled_back=True,
            scenario_id=scenario_id,
            request_id=resolved["request_id"],
            status=ScenarioAuditStatus.ROLLED_BACK,
            reason=sanitize_text(reason),
            baseline_forecast_id=resolved["baseline_forecast_id"],
            baseline_preserved=baseline_status
            in (BaselineForecastStatus.COMPLETED, BaselineForecastStatus.CREATED),
            baseline_status=baseline_status,
            audit_event_recorded=audit_event is not None,
            audit_history_preserved=True,
            partial_results_discarded=True,
            locks_released=locks_released,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _settle_baseline(
        self,
        *,
        stage: ScenarioStage,
        baseline_forecast_id: Optional[str],
        error: ScenarioPlannerError,
        notes: list[str],
    ) -> Tuple[Optional[BaselineForecastStatus], bool]:
        """
        Decide what happens to the baseline for this failure stage.

        Baseline creation failure marks the checkpoint ``failed``. Every later
        stage leaves a completed baseline exactly as it is.
        """
        if not baseline_forecast_id:
            return None, False

        if stage is ScenarioStage.BASELINE_CREATION:
            try:
                checkpoint = self.checkpoint_service.mark_failed(
                    baseline_forecast_id,
                    error_code=error.error_code,
                    error_message=sanitize_text(error.message),
                )
                notes.append("Baseline checkpoint was marked failed.")
                return checkpoint.status, False
            except (CheckpointError, DatabaseUnavailableError) as exc:
                logger.error(
                    "Could not mark baseline %s failed: %s",
                    baseline_forecast_id,
                    getattr(exc, "message", exc),
                )
                return None, False

        status = self._read_baseline_status(baseline_forecast_id)
        preserved = status is BaselineForecastStatus.COMPLETED
        if preserved:
            notes.append(
                "The baseline forecast completed and remains available for reuse."
            )
        return status, preserved

    def _read_baseline_status(
        self, baseline_forecast_id: Optional[str]
    ) -> Optional[BaselineForecastStatus]:
        """Current baseline status, or None when unavailable."""
        if not baseline_forecast_id:
            return None
        try:
            checkpoint = self.checkpoint_service.get(baseline_forecast_id)
        except (CheckpointError, DatabaseUnavailableError) as exc:
            logger.error(
                "Could not read baseline %s: %s",
                baseline_forecast_id,
                getattr(exc, "message", exc),
            )
            return None
        return checkpoint.status if checkpoint else None

    def _lookup_scenario_events(self, scenario_id: str) -> list:
        """Prior audit events for a scenario, tolerating a database outage."""
        try:
            return self.audit_service.get_by_scenario_id(scenario_id)
        except DatabaseUnavailableError as exc:
            logger.error(
                "Could not read audit history for scenario %s: %s", scenario_id, exc.message
            )
            return []

    @staticmethod
    def _resolve_identity(
        events: list,
        *,
        request_id: Optional[str],
        store_id: Optional[str],
        model_version: Optional[str],
        scenario_type: Optional[str],
        baseline_forecast_id: Optional[str],
    ) -> Dict[str, Any]:
        """
        Fill rollback metadata from the scenario's own history where possible.

        Explicit arguments win; otherwise the original attempt supplies them, so
        a rollback row is never written with placeholder identity when the real
        values are on record.
        """
        origin = events[0] if events else None
        return {
            "request_id": request_id
            or (origin.request_id if origin else None)
            or "rollback",
            "store_id": store_id or (origin.store_id if origin else None) or "unknown",
            "model_version": model_version
            or (origin.model_version if origin else None)
            or "unknown",
            "scenario_type": scenario_type
            or (origin.scenario_type if origin else None)
            or "unknown",
            "baseline_forecast_id": baseline_forecast_id
            or (origin.baseline_forecast_id if origin else None),
            "request_payload": (origin.request_payload if origin else None) or {},
        }

    def _release_lock(self, lock_key: Optional[str], request_id: Optional[str]) -> bool:
        """Release a scenario lock when one was taken."""
        if not lock_key:
            return False
        return self.lock_registry.release(lock_key, request_id)
