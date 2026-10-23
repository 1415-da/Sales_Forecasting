"""
Repository for persisted scenario forecasts.

All scenario database access goes through here: the services and routes never
build a session or a statement themselves. Every SQLAlchemy failure is
translated into ``DatabaseUnavailableError`` so callers only ever see the
project's own error taxonomy, never a driver exception carrying a DSN.

Nothing in this repository deletes a row. Expiry and rollback are status
transitions, which keeps scenario history auditable.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from api.core.errors import DatabaseUnavailableError, sanitize_text
from api.db.models.scenario import (
    EXPIRABLE_STATUSES,
    Base,
    ScenarioDailyForecast,
    ScenarioForecast,
    ScenarioForecastStatus,
)
from api.services.forecast_checkpoint_service import resolve_database_url

logger = logging.getLogger(__name__)

DEFAULT_SCENARIO_TTL_HOURS = 24
ERROR_MESSAGE_MAX_LENGTH = 4000

#: Distinguishes "ttl not specified" (use the default) from an explicit
#: ``ttl=None``, which means scenarios never expire.
_UNSET = object()


class BaseRepository:
    """
    Shared engine/session plumbing.

    The engine is created lazily, so constructing a repository never opens a
    connection; a misconfigured database surfaces on first use, not at import.
    """

    def __init__(
        self,
        engine: Optional[Any] = None,
        database_url: Optional[str] = None,
        session_factory: Optional[sessionmaker] = None,
        create_tables: bool = False,
    ) -> None:
        self._engine = engine
        self._database_url = database_url
        self._session_factory = session_factory
        if create_tables:
            self.create_all()

    @property
    def engine(self) -> Any:
        """The SQLAlchemy engine, created on first use."""
        if self._engine is None:
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
        """Create every scenario table that does not yet exist."""
        try:
            Base.metadata.create_all(self.engine)
        except SQLAlchemyError as exc:
            raise self._unavailable("create scenario tables", exc) from exc

    @staticmethod
    def _unavailable(action: str, exc: Exception) -> DatabaseUnavailableError:
        """Wrap a driver failure, keeping its raw text out of any response."""
        logger.error("Scenario database error while trying to %s: %s", action, exc)
        return DatabaseUnavailableError(
            details={"action": action, "error": sanitize_text(str(exc))}
        )


class ScenarioRepository(BaseRepository):
    """Reads and writes scenario comparisons and their daily rows."""

    def __init__(
        self,
        engine: Optional[Any] = None,
        database_url: Optional[str] = None,
        session_factory: Optional[sessionmaker] = None,
        create_tables: bool = False,
        ttl: Any = _UNSET,
    ) -> None:
        """Pass ``ttl=None`` to disable expiry; omit it for the default window."""
        super().__init__(
            engine=engine,
            database_url=database_url,
            session_factory=session_factory,
            create_tables=create_tables,
        )
        self.ttl: Optional[timedelta] = (
            timedelta(hours=DEFAULT_SCENARIO_TTL_HOURS) if ttl is _UNSET else ttl
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_scenario(
        self,
        *,
        scenario_id: str,
        store_id: str,
        model_version: str,
        model_stage: str,
        scenario_type: str,
        horizon_days: int,
        forecast_type: str,
        request_payload: Dict[str, Any],
        baseline_forecast_id: Optional[str] = None,
        status: ScenarioForecastStatus = ScenarioForecastStatus.CREATED,
        response_summary: Optional[Dict[str, Any]] = None,
        expires_at: Optional[datetime] = None,
        created_at: Optional[datetime] = None,
    ) -> ScenarioForecast:
        """
        Insert a scenario row.

        Written with status ``created`` before inference runs, so a crashed
        request leaves a diagnosable row rather than nothing at all.
        ``created_at`` defaults to now; a backfill passes the original time.
        """
        now = created_at or datetime.now(timezone.utc)
        if expires_at is None and self.ttl is not None:
            expires_at = now + self.ttl

        row = ScenarioForecast(
            id=uuid.uuid4(),
            scenario_id=scenario_id,
            baseline_forecast_id=baseline_forecast_id,
            store_id=store_id,
            model_version=model_version,
            model_stage=model_stage,
            scenario_type=scenario_type,
            horizon_days=horizon_days,
            status=status.value,
            forecast_type=forecast_type,
            request_payload=request_payload,
            response_summary=response_summary,
            created_at=now,
            completed_at=now if status is ScenarioForecastStatus.COMPLETED else None,
            expires_at=expires_at,
        )

        try:
            with self.session_factory() as session:
                session.add(row)
                session.commit()
                session.refresh(row)
                return row
        except SQLAlchemyError as exc:
            raise self._unavailable("insert scenario", exc) from exc

    def update_scenario_status(
        self,
        scenario_id: str,
        status: ScenarioForecastStatus,
        response_summary: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> Optional[ScenarioForecast]:
        """
        Move a scenario to a new status.

        ``completed_at`` is stamped only on the transition to ``completed``, so
        it always means "the comparison finished", never "something happened".
        Returns None when the scenario does not exist.
        """
        try:
            with self.session_factory() as session:
                row = self._fetch(session, scenario_id)
                if row is None:
                    return None

                row.status = status.value
                if status is ScenarioForecastStatus.COMPLETED:
                    row.completed_at = datetime.now(timezone.utc)
                if response_summary is not None:
                    row.response_summary = response_summary
                if error_code is not None:
                    row.error_code = error_code
                if error_message is not None:
                    row.error_message = error_message[:ERROR_MESSAGE_MAX_LENGTH]

                session.commit()
                session.refresh(row)
                return row
        except SQLAlchemyError as exc:
            raise self._unavailable(f"update scenario to {status.value}", exc) from exc

    def save_daily_forecasts(
        self, scenario_id: str, daily_rows: Sequence[Dict[str, Any]]
    ) -> List[ScenarioDailyForecast]:
        """
        Replace the daily rows for a scenario.

        Idempotent by design: a retried save produces one row per date rather
        than duplicates, which the unique ``(scenario_id, forecast_date)`` index
        would otherwise reject.
        """
        if not daily_rows:
            return []

        try:
            with self.session_factory() as session:
                existing = session.scalars(
                    select(ScenarioDailyForecast).where(
                        ScenarioDailyForecast.scenario_id == scenario_id
                    )
                ).all()
                for row in existing:
                    session.delete(row)
                session.flush()

                created = [
                    ScenarioDailyForecast(
                        id=uuid.uuid4(),
                        scenario_id=scenario_id,
                        forecast_date=self._as_date(item["forecast_date"]),
                        baseline_p10=_as_float(item.get("baseline_p10")),
                        baseline_p50=_as_float(item.get("baseline_p50")),
                        baseline_p90=_as_float(item.get("baseline_p90")),
                        scenario_p10=_as_float(item.get("scenario_p10")),
                        scenario_p50=_as_float(item.get("scenario_p50")),
                        scenario_p90=_as_float(item.get("scenario_p90")),
                        difference_p50=_as_float(item.get("difference_p50")),
                        difference_pct_p50=_as_float(item.get("difference_pct_p50")),
                        created_at=datetime.now(timezone.utc),
                    )
                    for item in daily_rows
                ]
                session.add_all(created)
                session.commit()
                for row in created:
                    session.refresh(row)
                return created
        except SQLAlchemyError as exc:
            raise self._unavailable("save daily forecasts", exc) from exc

    def rollback_scenario(
        self, scenario_id: str, reason: str
    ) -> Optional[ScenarioForecast]:
        """
        Mark a scenario rolled back.

        The row, its daily forecasts, and its audit events all survive: a
        rollback records that a result is withdrawn, not that it never existed.
        The reason is stored in ``error_message`` because the specified schema
        has no dedicated column for it.
        """
        return self.update_scenario_status(
            scenario_id,
            ScenarioForecastStatus.ROLLED_BACK,
            error_message=sanitize_text(reason),
        )

    def expire_old_scenarios(self, now: Optional[datetime] = None) -> int:
        """
        Mark every past-expiry scenario as expired. Returns the count changed.

        Only ``created`` and ``completed`` rows are touched, so a failed or
        rolled-back scenario keeps the status that explains it.
        """
        moment = now or datetime.now(timezone.utc)
        try:
            with self.session_factory() as session:
                stmt = select(ScenarioForecast).where(
                    ScenarioForecast.status.in_(EXPIRABLE_STATUSES),
                    ScenarioForecast.expires_at.is_not(None),
                    ScenarioForecast.expires_at <= moment,
                )
                rows = list(session.scalars(stmt).all())
                for row in rows:
                    row.status = ScenarioForecastStatus.EXPIRED.value
                session.commit()
                return len(rows)
        except SQLAlchemyError as exc:
            raise self._unavailable("expire scenarios", exc) from exc

    def save_comparison(
        self,
        comparison: Dict[str, Any],
        request_payload: Dict[str, Any],
        *,
        created_at: Optional[datetime] = None,
    ) -> ScenarioForecast:
        """
        Persist a completed comparison and its daily rows.

        ``comparison`` is a ``ScenarioComparisonResponse`` dumped to JSON.
        Idempotent: an existing row for the scenario id is reused and its daily
        rows are replaced, so a repeated save or backfill never duplicates.
        """
        scenario_id = comparison["scenario_id"]
        daily = comparison.get("daily_forecasts") or []
        row = self.get_scenario(scenario_id)
        if row is None:
            row = self.create_scenario(
                scenario_id=scenario_id,
                baseline_forecast_id=comparison.get("baseline_forecast_id"),
                store_id=comparison.get("store_id") or request_payload["store_id"],
                model_version=comparison["model_version"],
                model_stage=comparison["model_stage"],
                scenario_type=comparison["scenario_type"],
                horizon_days=int(request_payload.get("horizon_days") or len(daily)),
                forecast_type=comparison.get("forecast_type") or "predictive_what_if",
                request_payload=request_payload,
                status=ScenarioForecastStatus.COMPLETED,
                response_summary=comparison,
                created_at=created_at,
            )
        self.save_daily_forecasts(
            scenario_id,
            [
                {
                    "forecast_date": item["date"],
                    # Point forecasts stand in for the median when the model
                    # produced no quantiles.
                    "baseline_p10": item.get("baseline_p10"),
                    "baseline_p50": _first_present(
                        item.get("baseline_p50"), item.get("baseline_forecast")
                    ),
                    "baseline_p90": item.get("baseline_p90"),
                    "scenario_p10": item.get("scenario_p10"),
                    "scenario_p50": _first_present(
                        item.get("scenario_p50"), item.get("scenario_forecast")
                    ),
                    "scenario_p90": item.get("scenario_p90"),
                    "difference_p50": _first_present(
                        item.get("difference_p50"), item.get("daily_difference")
                    ),
                    "difference_pct_p50": item.get("daily_difference_pct"),
                }
                for item in daily
            ],
        )
        return row

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_scenario(self, scenario_id: str) -> Optional[ScenarioForecast]:
        """One scenario by id, or None."""
        try:
            with self.session_factory() as session:
                return self._fetch(session, scenario_id)
        except SQLAlchemyError as exc:
            raise self._unavailable("read scenario", exc) from exc

    def get_daily_forecasts(self, scenario_id: str) -> List[ScenarioDailyForecast]:
        """Daily rows for a scenario, in date order."""
        try:
            with self.session_factory() as session:
                stmt = (
                    select(ScenarioDailyForecast)
                    .where(ScenarioDailyForecast.scenario_id == scenario_id)
                    .order_by(ScenarioDailyForecast.forecast_date)
                )
                return list(session.scalars(stmt).all())
        except SQLAlchemyError as exc:
            raise self._unavailable("read daily forecasts", exc) from exc

    def list_scenarios_by_store(
        self,
        store_id: str,
        limit: int = 50,
        statuses: Optional[Iterable[ScenarioForecastStatus]] = None,
    ) -> List[ScenarioForecast]:
        """Most recent scenarios for a store, newest first."""
        try:
            with self.session_factory() as session:
                stmt = select(ScenarioForecast).where(
                    ScenarioForecast.store_id == store_id
                )
                if statuses:
                    stmt = stmt.where(
                        ScenarioForecast.status.in_([item.value for item in statuses])
                    )
                stmt = stmt.order_by(ScenarioForecast.created_at.desc()).limit(limit)
                return list(session.scalars(stmt).all())
        except SQLAlchemyError as exc:
            raise self._unavailable("list scenarios", exc) from exc

    def list_expired_scenarios(self, store_id: Optional[str] = None) -> List[ScenarioForecast]:
        """Scenarios already marked expired."""
        try:
            with self.session_factory() as session:
                stmt = select(ScenarioForecast).where(
                    ScenarioForecast.status == ScenarioForecastStatus.EXPIRED.value
                )
                if store_id:
                    stmt = stmt.where(ScenarioForecast.store_id == store_id)
                stmt = stmt.order_by(ScenarioForecast.created_at.desc())
                return list(session.scalars(stmt).all())
        except SQLAlchemyError as exc:
            raise self._unavailable("list expired scenarios", exc) from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch(session: Session, scenario_id: str) -> Optional[ScenarioForecast]:
        """Load one scenario row by its public id."""
        stmt = select(ScenarioForecast).where(ScenarioForecast.scenario_id == scenario_id)
        return session.scalars(stmt).first()

    @staticmethod
    def _as_date(value: Any) -> date:
        """Coerce a date-ish value to ``datetime.date``."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))


def _as_float(value: Any) -> Optional[float]:
    """Coerce to float, preserving None."""
    return None if value is None else float(value)


def _first_present(*values: Any) -> Any:
    """The first value that is not None, or None."""
    return next((value for value in values if value is not None), None)
