"""
Training requests: submit to Airflow, track the run, collect the candidates.

The request row is written *before* Airflow is called, under a dag-run id
derived from the row's id. That ordering is what makes every failure mode
recoverable by looking the run up:

* a double click sends the same idempotency key and gets the same row back;
* a timeout leaves the row ``submission_uncertain``; the next status read asks
  Airflow for that exact run id and either finds it or re-submits it, and a
  re-submit of a run that did land is refused as a duplicate, never doubled;
* a connection failure leaves it ``pending`` and is retried the same way.

Nothing here waits for training. ``sync`` is called on each status read and
moves the row forward from whatever Airflow reports.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import Settings, get_settings
from api.core.errors import (
    DatasetNotReadyError,
    IdempotencyKeyConflictError,
    OrchestratorRejectedError,
    OrchestratorUnavailableError,
    PermissionDeniedError,
    ScenarioValidationError,
    TrainingRequestNotFoundError,
    sanitize_text,
)
from api.core.rbac import Permission, has_permission
from api.db.base import as_utc, utcnow
from api.db.models.training import (
    DatasetStatus,
    DecisionKind,
    DeploymentDecision,
    DeploymentState,
    TrainingDataset,
    TrainingRequest,
    TrainingStatus,
)
from api.db.models.user import User
from api.services.airflow_client import (
    AirflowClient,
    AirflowConflict,
    AirflowRejected,
    AirflowTimeout,
    AirflowUnavailable,
)
from training import storage
from training.configs import DEPLOYMENT_POLICY, TRAINING_CONFIGS, get_config

logger = logging.getLogger(__name__)

IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
SUMMARY_TASK = "summarize_training_request_task"
GATE_TASK = "quality_gate_task"

#: Task order of the training DAG, for display.
TASK_ORDER = (
    "extract_data_task",
    "validate_data_task",
    "train_models_task",
    "evaluate_models_task",
    "register_best_model_task",
    "transition_to_production_task",
    "quality_gate_task",
    "compare_with_production_task",
    "summarize_training_request_task",
    "generate_performance_report_task",
    "cleanup",
)

_AIRFLOW_RUN_STATES = {
    "queued": TrainingStatus.QUEUED,
    "running": TrainingStatus.RUNNING,
    "success": TrainingStatus.SUCCEEDED,
    "failed": TrainingStatus.FAILED,
}


def dag_run_id_for(request_id: uuid.UUID) -> str:
    """Deterministic, unique run id: one request can only ever be one run."""
    return f"training__{request_id.hex}"


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


class TrainingService:
    def __init__(
        self,
        session: Session,
        airflow: Optional[AirflowClient] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.airflow = airflow or AirflowClient(self.settings)

    # -- reads ---------------------------------------------------------------

    def get(self, request_id: Any) -> TrainingRequest:
        try:
            key = request_id if isinstance(request_id, uuid.UUID) else uuid.UUID(str(request_id))
        except (TypeError, ValueError):
            raise TrainingRequestNotFoundError("No training request with that id.")
        row = self.session.get(TrainingRequest, key)
        if row is None:
            raise TrainingRequestNotFoundError("No training request with that id.")
        return row

    def list(self, limit: int = 50) -> List[TrainingRequest]:
        return list(
            self.session.scalars(
                select(TrainingRequest).order_by(TrainingRequest.created_at.desc()).limit(limit)
            )
        )

    def find_candidate(
        self, model: str, version: str, run_id: Optional[str] = None
    ) -> Optional[TrainingRequest]:
        """
        The request that produced a registered model version, if any.

        Matched on the version's MLflow run id when it is known, so the decision
        is tied to the exact artifacts in the registry, not just to a number.
        """
        statement = (
            select(TrainingRequest)
            .where(TrainingRequest.status == TrainingStatus.SUCCEEDED.value)
            .order_by(TrainingRequest.created_at.desc())
        )
        if run_id:
            statement = statement.where(TrainingRequest.mlflow_run_id == run_id)
        for row in self.session.scalars(statement):
            for candidate in row.candidates or []:
                if candidate.get("model") == model and str(candidate.get("version")) == str(version):
                    return row
        return None

    def decisions(self, request: TrainingRequest) -> List[DeploymentDecision]:
        return list(
            self.session.scalars(
                select(DeploymentDecision)
                .where(DeploymentDecision.training_request_id == request.id)
                .order_by(DeploymentDecision.decided_at.desc())
            )
        )

    # -- submit --------------------------------------------------------------

    def submit(
        self,
        *,
        user: User,
        dataset: TrainingDataset,
        config_id: str,
        idempotency_key: str,
    ) -> tuple[TrainingRequest, bool]:
        """Create (or return the existing) request and start its run. ``(row, created)``."""
        if not IDEMPOTENCY_PATTERN.match(idempotency_key or ""):
            raise ScenarioValidationError(
                "Idempotency-Key must be 8-64 letters, digits, '-' or '_'.", field="idempotency_key"
            )
        existing = self.session.scalar(
            select(TrainingRequest).where(TrainingRequest.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if existing.dataset_id != dataset.id or existing.training_config_id != config_id:
                raise IdempotencyKeyConflictError(
                    "This idempotency key was already used for a different training request.",
                    field="idempotency_key",
                )
            return existing, False

        try:
            config = get_config(config_id)
        except KeyError:
            raise ScenarioValidationError(
                f"Unknown training configuration. Choose one of: {', '.join(TRAINING_CONFIGS)}.",
                field="training_config_id",
            )
        if not has_permission(user.role_enum, Permission(config.required_permission)):
            raise PermissionDeniedError(
                f"The '{config.label}' configuration requires an ML Engineer or Administrator.",
                field="training_config_id",
            )
        if dataset.status != DatasetStatus.READY.value or not dataset.manifest_object_key:
            raise DatasetNotReadyError(
                "Only a sealed (ready) dataset version can be trained on.", field="dataset_id"
            )

        # Checked before anything is recorded: a paused or missing DAG is a
        # configuration problem the user should see immediately.
        self._require_dag()

        request_id = uuid.uuid4()
        row = TrainingRequest(
            id=request_id,
            idempotency_key=idempotency_key,
            dataset_id=dataset.id,
            dataset_name=dataset.name,
            dataset_version=dataset.version,
            dataset_content_sha256=dataset.content_sha256,
            training_config_id=config.id,
            training_config=config.as_dict(),
            deployment_policy=DEPLOYMENT_POLICY,
            dag_id=self.settings.training_dag_id,
            dag_run_id=dag_run_id_for(request_id),
            status=TrainingStatus.PENDING.value,
            task_states=[],
            candidates=[],
            evaluation={},
            deployment_state=DeploymentState.NOT_READY.value,
            created_by_id=user.id,
            created_by_email=user.email,
            created_by_role=user.role,
        )
        self.session.add(row)
        # Committed before the network call, so a crash after Airflow accepted
        # the run still leaves a row that knows the run id.
        self.session.commit()
        self._trigger(row, dataset)
        self.session.commit()
        return row, True

    def conf_for(self, row: TrainingRequest, dataset: TrainingDataset) -> Dict[str, Any]:
        """The dag-run conf: small, server-resolved metadata only."""
        return {
            "training_request_id": row.id.hex,
            "dataset_id": dataset.id.hex,
            "dataset_version": dataset.version,
            "manifest_uri": storage.to_uri(dataset.manifest_object_key),
            "manifest_sha256": dataset.manifest_sha256,
            "content_sha256": dataset.content_sha256,
            "training_config_id": row.training_config_id,
            "deployment_policy": row.deployment_policy,
        }

    def _require_dag(self) -> None:
        try:
            dag = self.airflow.get_dag(self.settings.training_dag_id)
        except (AirflowUnavailable, AirflowTimeout) as exc:
            raise OrchestratorUnavailableError(
                "Airflow is not reachable, so training cannot be started right now."
            ) from exc
        except AirflowRejected as exc:
            raise OrchestratorRejectedError(str(exc)) from exc
        if dag.get("is_paused"):
            raise OrchestratorRejectedError(
                f"The training DAG '{self.settings.training_dag_id}' is paused in Airflow. "
                "Unpause it, then start training again."
            )

    def _trigger(self, row: TrainingRequest, dataset: TrainingDataset) -> None:
        conf = self.conf_for(row, dataset)
        note = f"Training request {row.id} by {row.created_by_email} ({row.training_config_id})"
        row.submitted_at = utcnow()
        try:
            run = self.airflow.trigger(row.dag_id, row.dag_run_id, conf, note)
        except AirflowConflict:
            run = self.airflow.get_run(row.dag_id, row.dag_run_id)
        except AirflowTimeout:
            row.status = TrainingStatus.SUBMISSION_UNCERTAIN.value
            row.failure_reason = (
                "Airflow did not confirm the submission in time; the run id will be "
                "checked and re-submitted if it never arrived."
            )
            return
        except AirflowUnavailable:
            row.status = TrainingStatus.PENDING.value
            row.failure_reason = "Airflow was unreachable; submission will be retried."
            return
        except AirflowRejected as exc:
            row.status = TrainingStatus.FAILED.value
            row.failure_reason = sanitize_text(f"Airflow refused the run: {exc}")
            row.finished_at = utcnow()
            return
        row.failure_reason = None
        self._apply_run(row, run)

    # -- sync ----------------------------------------------------------------

    def sync(self, row: TrainingRequest, dataset: Optional[TrainingDataset] = None) -> Optional[str]:
        """
        Bring the row up to date with Airflow. Returns a warning when Airflow
        could not be asked (the stored state is then shown as-is).
        """
        status = TrainingStatus(row.status)
        if status.is_terminal and row.last_synced_at and (
            status is TrainingStatus.FAILED or row.candidates
        ):
            return None
        try:
            run = self.airflow.get_run(row.dag_id, row.dag_run_id)
            if run is None:
                if status in (TrainingStatus.PENDING, TrainingStatus.SUBMISSION_UNCERTAIN):
                    submitted = as_utc(row.submitted_at) if row.submitted_at else None
                    wait = timedelta(seconds=self.settings.training_resubmit_after_seconds)
                    if submitted is None or utcnow() - submitted >= wait or status is TrainingStatus.PENDING:
                        dataset = dataset or self.session.get(TrainingDataset, row.dataset_id)
                        self._trigger(row, dataset)
                elif not status.is_terminal:
                    row.status = TrainingStatus.FAILED.value
                    row.failure_reason = "The Airflow run no longer exists."
                    row.finished_at = utcnow()
            else:
                self._apply_run(row, run)
                row.task_states = self._tasks(row)
                if TrainingStatus(row.status) is TrainingStatus.SUCCEEDED and not row.candidates:
                    self._collect(row)
                elif TrainingStatus(row.status) is TrainingStatus.FAILED:
                    row.failure_reason = self._failure_reason(row)
                    row.deployment_state = DeploymentState.NOT_READY.value
            row.last_synced_at = utcnow()
            self.session.flush()
            return None
        except (AirflowUnavailable, AirflowTimeout):
            return "Airflow is not reachable right now; showing the last known state."
        except AirflowRejected as exc:
            return sanitize_text(f"Airflow refused the status request: {exc}")

    def _apply_run(self, row: TrainingRequest, run: Optional[Dict[str, Any]]) -> None:
        if not run:
            return
        state = str(run.get("state") or "").lower()
        row.airflow_state = state or None
        mapped = _AIRFLOW_RUN_STATES.get(state, TrainingStatus.QUEUED)
        row.status = mapped.value
        row.started_at = _parse_time(run.get("start_date")) or row.started_at
        row.finished_at = _parse_time(run.get("end_date")) if mapped.is_terminal else None
        if mapped is not TrainingStatus.FAILED:
            row.failure_reason = None

    def _tasks(self, row: TrainingRequest) -> List[Dict[str, Any]]:
        order = {task: index for index, task in enumerate(TASK_ORDER)}
        tasks = []
        for item in self.airflow.task_instances(row.dag_id, row.dag_run_id):
            tasks.append(
                {
                    "task_id": item.get("task_id"),
                    "state": item.get("state") or "scheduled",
                    "try_number": item.get("try_number"),
                    "start_date": item.get("start_date"),
                    "end_date": item.get("end_date"),
                    "duration": item.get("duration"),
                }
            )
        return sorted(tasks, key=lambda item: order.get(item["task_id"], len(order)))

    def _failure_reason(self, row: TrainingRequest) -> str:
        failed = [task for task in row.task_states if task.get("state") in ("failed", "upstream_failed")]
        first = next((task for task in failed if task.get("state") == "failed"), None)
        if first is None:
            return "The training run failed."
        message = f"Task {first['task_id']} failed"
        if first.get("try_number"):
            message += f" (attempt {first['try_number']})"
        if first["task_id"] == GATE_TASK:
            message += ": no candidate passed the quality gate"
        return message + ". Open the Airflow run for the full log."

    def _collect(self, row: TrainingRequest) -> None:
        summary = self.airflow.xcom(row.dag_id, row.dag_run_id, SUMMARY_TASK)
        if not isinstance(summary, dict) or not summary.get("candidates"):
            row.failure_reason = "The run succeeded but reported no candidates."
            row.deployment_state = DeploymentState.NOT_READY.value
            return
        if summary.get("training_request_id") != row.id.hex:
            row.failure_reason = "The run's summary belongs to a different request."
            return
        row.mlflow_run_id = summary.get("mlflow_run_id")
        row.candidates = summary["candidates"]
        row.evaluation = summary.get("evaluation") or {}
        row.deployment_state = (
            DeploymentState.AWAITING_REVIEW.value
            if any(candidate.get("gate_passed") for candidate in row.candidates)
            else DeploymentState.BLOCKED.value
        )

    # -- decisions -----------------------------------------------------------

    def candidate(self, row: TrainingRequest, model: str, version: str) -> Dict[str, Any]:
        for item in row.candidates or []:
            if item.get("model") == model and str(item.get("version")) == str(version):
                return item
        raise ScenarioValidationError(
            f"{model} v{version} is not a candidate of this training request.", field="version"
        )

    def record_decision(
        self,
        *,
        row: TrainingRequest,
        user: User,
        kind: DecisionKind,
        model: str,
        version: str,
        reason: str,
        checks: Dict[str, Any],
        previous_production_version: Optional[str] = None,
    ) -> DeploymentDecision:
        decision = DeploymentDecision(
            training_request_id=row.id,
            decision=kind.value,
            model=model,
            version=str(version),
            reason=reason,
            previous_production_version=previous_production_version,
            checks=checks,
            decided_by_id=user.id,
            decided_by_email=user.email,
            decided_by_role=user.role,
        )
        self.session.add(decision)
        if kind is DecisionKind.APPROVED:
            row.deployment_state = DeploymentState.DEPLOYED.value
            row.deployed_model = model
            row.deployed_version = str(version)
        elif row.deployment_state != DeploymentState.DEPLOYED.value:
            row.deployment_state = DeploymentState.REJECTED.value
        self.session.flush()
        return decision

    def run_url(self, row: TrainingRequest) -> Optional[str]:
        return self.airflow.run_url(row.dag_id, row.dag_run_id)
