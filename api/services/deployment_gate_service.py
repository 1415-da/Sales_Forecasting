"""
Deployment gates for candidates produced by training requests.

Run by the existing promote endpoint before it moves the Production stage, so
there is still exactly one way to deploy a model. A candidate is deployable
only when all of these hold at the moment of approval:

1. its training run succeeded and its quality gate passed;
2. it describes the same business as the model it replaces -- same target,
   frequency and currency -- and serves every store the current Production
   version serves. A candidate for a different store domain would silently
   stop forecasts for the stores it lacks; scoped (per-store) deployment is
   not supported, so that is refused with the list of stores it would drop;
3. a smoke forecast through the real serving path -- registry, artifact
   download, feature builder, recursion -- produces finite values, using real
   history from the candidate's own dataset.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from api.core.errors import DeploymentBlockedError
from api.db.models.training import TrainingRequest, TrainingStatus
from api.schemas.forecast import HistoryObservation
from api.services.forecast_service import ForecastService
from api.services.training_dataset_service import TrainingDatasetService
from training import contract

logger = logging.getLogger(__name__)

SMOKE_HORIZON_DAYS = 7


class DeploymentGate:
    def __init__(
        self,
        forecast_service: ForecastService,
        datasets: TrainingDatasetService,
    ) -> None:
        self.forecasts = forecast_service
        self.datasets = datasets

    def evaluate(self, row: TrainingRequest, model: str, version: str) -> Dict[str, Any]:
        """All checks, as data. Raises nothing; ``passed`` says whether to deploy."""
        checks: List[Dict[str, Any]] = []

        def check(name: str, passed: bool, detail: str) -> None:
            checks.append({"check": name, "passed": bool(passed), "detail": detail})

        candidate = next(
            (
                item
                for item in row.candidates or []
                if item.get("model") == model and str(item.get("version")) == str(version)
            ),
            None,
        )
        check(
            "training_succeeded",
            row.status == TrainingStatus.SUCCEEDED.value and candidate is not None,
            "The run succeeded and registered this candidate."
            if candidate
            else "This version is not a candidate of a successful training run.",
        )
        check(
            "quality_gate",
            bool(candidate and candidate.get("gate_passed")),
            "Passed in the training run." if candidate and candidate.get("gate_passed") else "Failed or not run.",
        )

        compat = (row.evaluation or {}).get("compatibility") or {}
        policy = self.datasets.policy
        domain_ok = (
            compat.get("target") == contract.TARGET
            and compat.get("frequency") == contract.FREQUENCY
            and compat.get("currency") in policy.allowed_currencies
        )
        check(
            "business_domain",
            domain_ok,
            f"Target {compat.get('target')}, frequency {compat.get('frequency')}, currency {compat.get('currency')}."
            + ("" if domain_ok else " The platform serves daily store revenue in "
               + ", ".join(policy.allowed_currencies) + "."),
        )

        candidate_stores = set(compat.get("supported_stores") or [])
        current = self._production(model)
        if current is None:
            check("store_coverage", bool(candidate_stores), "No Production version to compare stores with.")
        else:
            bundle = self.forecasts.artifacts.try_load_bundle(current.run_id, model) if current.run_id else None
            served = set(bundle.supported_stores) if bundle else set()
            dropped = sorted(served - candidate_stores)
            check(
                "store_coverage",
                not dropped,
                f"Serves all {len(served)} stores the current version {current.version} serves."
                if not dropped
                else (
                    f"Would stop forecasts for {len(dropped)} store(s) the current version serves "
                    f"({', '.join(dropped[:10])}). Scoped deployment is not supported, so this "
                    "candidate cannot replace the shared model."
                ),
            )

        if all(item["passed"] for item in checks):
            passed, detail = self._smoke(row, model, version, sorted(candidate_stores))
            check("smoke_forecast", passed, detail)
        else:
            check("smoke_forecast", False, "Skipped: an earlier check failed.")

        return {
            "passed": all(item["passed"] for item in checks),
            "checks": checks,
            "previous_production_version": current.version if current else None,
        }

    def require(self, row: TrainingRequest, model: str, version: str) -> Dict[str, Any]:
        result = self.evaluate(row, model, version)
        if not result["passed"]:
            failed = [item for item in result["checks"] if not item["passed"]]
            raise DeploymentBlockedError(
                f"{model} v{version} cannot be deployed: " + failed[0]["detail"],
                field="version",
                details={"checks": result["checks"]},
            )
        return result

    def _production(self, model: str):
        for item in self.forecasts.registry.list_versions(model):
            if item.is_production:
                return item
        return None

    def _smoke(self, row: TrainingRequest, model: str, version: str, stores: List[str]) -> tuple:
        try:
            resolved = next(
                item for item in self.forecasts.registry.list_versions(model) if item.version == str(version)
            )
        except StopIteration:
            return False, f"Version {version} is not in the registry."
        if not stores:
            return False, "The candidate reports no supported stores."
        try:
            dataset = self.datasets.get(row.dataset_id)
            days = max(self.forecasts.settings.recommended_history_days, self.forecasts.settings.min_history_days)
            history = [
                HistoryObservation(date=item["date"], sales=max(0.0, item["sales"]))
                for item in self.datasets.history(dataset, stores[0], days)
            ]
            projection = self.forecasts.project(
                resolved=resolved, history=history, store_id=stores[0], horizon_days=SMOKE_HORIZON_DAYS
            )
        except Exception as exc:  # noqa: BLE001 - any failure blocks deployment
            logger.warning("Smoke forecast failed for %s v%s: %s", model, version, exc)
            return False, f"The serving path could not forecast with it: {type(exc).__name__}: {exc}"
        values = [value for _, value in projection.predictions]
        ok = len(values) == SMOKE_HORIZON_DAYS and all(math.isfinite(v) for v in values)
        return ok, (
            f"Loaded through the serving path and forecast {SMOKE_HORIZON_DAYS} days for {stores[0]} "
            f"from its dataset history ({min(values):,.0f} to {max(values):,.0f})."
            if ok
            else "The serving path returned non-finite or missing values."
        )


def dataset_deployment_compatibility(
    forecasts: ForecastService,
    allowed_currencies: List[str],
    stores: List[str],
    currency: str,
) -> Dict[str, Any]:
    """
    Whether a model trained on this dataset could replace the current one.

    Shown at validation time so nobody discovers it after training: a dataset
    can be perfectly trainable and still describe a different business from
    the serving model.
    """
    model = forecasts.settings.default_model
    current = next((item for item in forecasts.registry.list_versions(model) if item.is_production), None)
    reasons: List[str] = []
    if currency not in allowed_currencies:
        reasons.append(
            f"Revenue is in {currency or 'an unconfirmed currency'}; the serving model forecasts "
            f"{', '.join(allowed_currencies)}."
        )
    served: List[str] = []
    if current is not None and current.run_id:
        bundle = forecasts.artifacts.try_load_bundle(current.run_id, model)
        served = list(bundle.supported_stores) if bundle else []
        missing = sorted(set(served) - set(stores))
        if missing:
            reasons.append(
                f"The current Production model ({model} v{current.version}) serves {len(served)} stores; "
                f"{len(missing)} of them are not in this dataset (e.g. {', '.join(missing[:5])}). "
                "Scoped deployment is not supported, so a model trained on it cannot replace the shared model."
            )
    return {
        "compatible": not reasons,
        "production_model": f"{model} v{current.version}" if current else None,
        "reasons": reasons,
        "note": "This does not block training; it would block deployment of the resulting candidate.",
    }
