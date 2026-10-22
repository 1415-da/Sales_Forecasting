"""
Background processing of training uploads.

A bounded thread pool inside the API process: at most
``TRAINING_MAX_CONCURRENT_JOBS`` uploads are parsed and validated at once,
the rest wait their turn. Status lives in the database row, not in memory, so
the browser polls the dataset and a page reload or navigation loses nothing.

If the API restarts mid-job, the thread is gone but the row still says
``processing``. ``recover_interrupted`` runs at start-up and marks those rows
``failed`` with an explanation; their original files are already in object
storage, so "Retry processing" re-runs them from there.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from sqlalchemy import update

from api.db.base import utcnow
from api.db.models.training import DatasetStatus, TrainingDataset
from api.db.session import session_scope
from data_ingestion.limits import UploadLimits

logger = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = (
    "PROCESSING_FAILED: Processing was interrupted by a service restart. "
    "The uploaded files are stored; retry processing."
)

_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=UploadLimits.from_env().max_concurrent_jobs,
                thread_name_prefix="training-ingest",
            )
        return _executor


def run(dataset_id: Any, service_factory: Callable[[Any], Any]) -> None:
    """Process one dataset in its own transaction."""
    try:
        with session_scope() as session:
            service = service_factory(session)
            service.process(service.get(dataset_id))
    except Exception:  # noqa: BLE001 - the row records failures; this is a backstop
        logger.exception("Background processing of dataset %s crashed", dataset_id)


def submit(dataset_id: Any, service_factory: Callable[[Any], Any]) -> Future:
    return _pool().submit(run, dataset_id, service_factory)


def recover_interrupted() -> int:
    """Mark sessions left ``processing`` by a previous process as failed."""
    with session_scope() as session:
        result = session.execute(
            update(TrainingDataset)
            .where(TrainingDataset.status == DatasetStatus.PROCESSING.value)
            .values(
                status=DatasetStatus.FAILED.value,
                processing_error=INTERRUPTED_MESSAGE,
                processing_finished_at=utcnow(),
            )
        )
        count = result.rowcount or 0
    if count:
        logger.warning("Marked %d interrupted training upload(s) as failed", count)
    return count
