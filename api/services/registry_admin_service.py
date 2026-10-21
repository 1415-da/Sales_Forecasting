"""
Model registry mutations: promote, archive, roll back, and compare.

Why this talks to MLflow over REST
----------------------------------
The deployed tracking server is 2.9.2 while the installed client may be 3.x. In
that pairing the client's ``transition_model_version_stage`` has been removed,
so a promotion silently did nothing -- which is exactly the failure that left
the registry with no Production version for weeks. The REST endpoint
``POST /api/2.0/mlflow/model-versions/transition-stage`` exists on the server
and is what actually works, so it is called directly and its response is
*verified* rather than assumed.

Every mutation here is verified by re-reading the version after the call. A
registry operation that reports success without being checked is how the
original bug survived.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from api.config import Settings, get_settings
from api.core.errors import (
    InvalidStateTransitionError,
    MLflowUnavailableError,
    ModelNotFoundError,
    RegistryOperationFailedError,
)
from api.services.model_registry_service import (
    PRODUCTION_STAGE,
    ModelRegistryService,
    ResolvedModelVersion,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 20.0

STAGE_NONE = "None"
STAGE_STAGING = "Staging"
STAGE_PRODUCTION = "Production"
STAGE_ARCHIVED = "Archived"

#: Metrics where a smaller value is better. Needed so a comparison can say
#: "improved" rather than only "changed".
LOWER_IS_BETTER = frozenset({"rmse", "mae", "mape", "wape"})
#: Metrics where a larger value is better.
HIGHER_IS_BETTER = frozenset({"r2"})


class RegistryAction(str, Enum):
    """A registry mutation."""

    PROMOTE = "promote"
    ARCHIVE = "archive"
    ROLLBACK = "rollback"


@dataclass
class MetricDelta:
    """One metric compared between two versions."""

    metric: str
    version_a: Optional[float]
    version_b: Optional[float]
    delta: Optional[float] = None
    delta_pct: Optional[float] = None
    #: "improved", "regressed", "unchanged", or "incomparable".
    verdict: str = "incomparable"
    lower_is_better: bool = True


@dataclass
class VersionComparison:
    """Two versions, side by side."""

    version_a: ResolvedModelVersion
    version_b: ResolvedModelVersion
    metrics: List[MetricDelta] = field(default_factory=list)
    summary: str = ""
    improved_count: int = 0
    regressed_count: int = 0


@dataclass
class MutationResult:
    """The outcome of a verified registry mutation."""

    action: RegistryAction
    model: str
    version: str
    previous_stage: str
    new_stage: str
    archived_versions: List[str] = field(default_factory=list)
    verified: bool = False


class RegistryAdminService:
    """Verified registry mutations and version comparison."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[ModelRegistryService] = None,
        transport: Optional[Any] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry or ModelRegistryService(self.settings)
        #: Injectable so the mutation logic, the verification step, and the
        #: error contract can all be tested without an MLflow server.
        self._transport = transport or self._rest_call

    # -- comparison --------------------------------------------------------

    def compare(
        self, *, model: str, version_a: str, version_b: str
    ) -> VersionComparison:
        """
        Compare two versions of one model, metric by metric.

        Direction matters: ``version_a`` is the incumbent and ``version_b`` the
        candidate, so "improved" means the candidate is better. Comparing the
        two in the other order would invert every verdict.
        """
        resolved_a = self._require_version(model, version_a)
        resolved_b = self._require_version(model, version_b)

        names = sorted(set(resolved_a.metrics) | set(resolved_b.metrics))
        deltas: List[MetricDelta] = []
        improved = regressed = 0

        for name in names:
            value_a = resolved_a.metrics.get(name)
            value_b = resolved_b.metrics.get(name)
            lower_better = name not in HIGHER_IS_BETTER
            delta = MetricDelta(
                metric=name,
                version_a=value_a,
                version_b=value_b,
                lower_is_better=lower_better,
            )

            if value_a is not None and value_b is not None:
                delta.delta = value_b - value_a
                if value_a != 0:
                    delta.delta_pct = (value_b - value_a) / abs(value_a)
                if delta.delta == 0:
                    delta.verdict = "unchanged"
                else:
                    better = (delta.delta < 0) if lower_better else (delta.delta > 0)
                    delta.verdict = "improved" if better else "regressed"
                    if better:
                        improved += 1
                    else:
                        regressed += 1
            deltas.append(delta)

        return VersionComparison(
            version_a=resolved_a,
            version_b=resolved_b,
            metrics=deltas,
            improved_count=improved,
            regressed_count=regressed,
            summary=self._comparison_summary(
                model=model,
                version_a=version_a,
                version_b=version_b,
                improved=improved,
                regressed=regressed,
                comparable=improved + regressed,
            ),
        )

    @staticmethod
    def _comparison_summary(
        *,
        model: str,
        version_a: str,
        version_b: str,
        improved: int,
        regressed: int,
        comparable: int,
    ) -> str:
        """A sentence stating what the comparison shows, without overclaiming."""
        if comparable == 0:
            return (
                f"Version {version_b} cannot be compared with {version_a}: no "
                "metric is recorded for both."
            )
        if improved and not regressed:
            return (
                f"Version {version_b} of '{model}' improves on {version_a} across "
                f"all {improved} comparable metric(s)."
            )
        if regressed and not improved:
            return (
                f"Version {version_b} of '{model}' is worse than {version_a} "
                f"across all {regressed} comparable metric(s)."
            )
        return (
            f"Version {version_b} of '{model}' improves {improved} metric(s) and "
            f"regresses {regressed} against {version_a}. Review the breakdown "
            "before promoting."
        )

    # -- mutations ---------------------------------------------------------

    def promote(
        self, *, model: str, version: str, archive_existing: bool = True
    ) -> MutationResult:
        """
        Move a version to Production.

        ``archive_existing`` defaults to true so exactly one version is ever in
        Production. Two Production versions would make "which model served this
        forecast" unanswerable, which is worse than a brief gap.
        """
        resolved = self._require_version(model, version)
        if resolved.stage == STAGE_PRODUCTION:
            raise InvalidStateTransitionError(
                f"Version {version} of '{model}' is already in Production.",
                field="version",
            )

        previous_production = [
            item.version
            for item in self.registry.list_versions(model)
            if item.is_production and item.version != version
        ]

        self._transition(
            model=model,
            version=version,
            stage=STAGE_PRODUCTION,
            archive_existing=archive_existing,
        )
        verified = self._verify_stage(model, version, STAGE_PRODUCTION)

        return MutationResult(
            action=RegistryAction.PROMOTE,
            model=model,
            version=version,
            previous_stage=resolved.stage,
            new_stage=STAGE_PRODUCTION,
            archived_versions=previous_production if archive_existing else [],
            verified=verified,
        )

    def archive(self, *, model: str, version: str) -> MutationResult:
        """
        Move a version to Archived.

        Refused for the current Production version: archiving what is serving
        would leave the API with nothing to load. Promote a replacement first,
        which archives the old one as part of the same transition.
        """
        resolved = self._require_version(model, version)
        if resolved.stage == STAGE_ARCHIVED:
            raise InvalidStateTransitionError(
                f"Version {version} of '{model}' is already archived.",
                field="version",
            )
        if resolved.stage == STAGE_PRODUCTION:
            raise InvalidStateTransitionError(
                f"Version {version} is serving Production traffic. Promote a "
                "replacement first -- that archives this one as part of the "
                "same transition.",
                field="version",
            )

        self._transition(
            model=model, version=version, stage=STAGE_ARCHIVED, archive_existing=False
        )
        verified = self._verify_stage(model, version, STAGE_ARCHIVED)

        return MutationResult(
            action=RegistryAction.ARCHIVE,
            model=model,
            version=version,
            previous_stage=resolved.stage,
            new_stage=STAGE_ARCHIVED,
            verified=verified,
        )

    def rollback(self, *, model: str, version: str) -> MutationResult:
        """
        Return a previous version to Production.

        Mechanically a promotion; kept separate because the intent differs and
        the audit trail should say "rolled back", not "promoted". Requires that
        something else is currently in Production -- with nothing serving, this
        is a promotion, and calling it a rollback would misdescribe it.
        """
        resolved = self._require_version(model, version)
        if resolved.stage == STAGE_PRODUCTION:
            raise InvalidStateTransitionError(
                f"Version {version} of '{model}' is already in Production.",
                field="version",
            )

        current = [
            item for item in self.registry.list_versions(model) if item.is_production
        ]
        if not current:
            raise InvalidStateTransitionError(
                f"'{model}' has no Production version to roll back from. Use "
                "promote instead.",
                field="version",
            )

        self._transition(
            model=model,
            version=version,
            stage=STAGE_PRODUCTION,
            archive_existing=True,
        )
        verified = self._verify_stage(model, version, STAGE_PRODUCTION)

        return MutationResult(
            action=RegistryAction.ROLLBACK,
            model=model,
            version=version,
            previous_stage=resolved.stage,
            new_stage=STAGE_PRODUCTION,
            archived_versions=[item.version for item in current],
            verified=verified,
        )

    # -- transport ---------------------------------------------------------

    def _transition(
        self, *, model: str, version: str, stage: str, archive_existing: bool
    ) -> None:
        """Apply a stage transition through the tracking server's REST API."""
        registered_name = self.settings.registered_model_name(model)
        try:
            self._transport(
                "/api/2.0/mlflow/model-versions/transition-stage",
                {
                    "name": registered_name,
                    "version": str(version),
                    "stage": stage,
                    "archive_existing_versions": bool(archive_existing),
                },
            )
        except (MLflowUnavailableError, RegistryOperationFailedError):
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed error
            raise RegistryOperationFailedError(
                "The stage transition could not be applied.",
                details={"model": model, "version": version, "target_stage": stage},
            ) from exc

    def _verify_stage(self, model: str, version: str, expected: str) -> bool:
        """
        Re-read the version and confirm the stage actually changed.

        The whole reason this method exists: a transition that reported success
        without being checked is what previously left the registry with no
        Production version while the DAG logged a successful promotion.
        """
        try:
            # Build a fresh service so the verification is not answered from
            # the run cache populated before the mutation.
            fresh = ModelRegistryService(self.settings)
            for item in fresh.list_versions(model):
                if item.version == str(version):
                    if item.stage == expected:
                        return True
                    logger.error(
                        "Stage transition for %s v%s did not take effect: stage is "
                        "%r, expected %r",
                        model,
                        version,
                        item.stage,
                        expected,
                    )
                    return False
            logger.error(
                "Version %s of %s disappeared after a transition", version, model
            )
            return False
        except Exception as exc:  # noqa: BLE001 - verification is best-effort
            logger.warning("Could not verify the stage transition: %s", exc)
            return False

    def _rest_call(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST to the tracking server and return the decoded response."""
        base = (self.settings.mlflow_tracking_uri or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise MLflowUnavailableError(
                "The model tracking service is not configured for registry "
                "operations.",
                details={"reason": "MLFLOW_TRACKING_URI is not an HTTP URL"},
            )

        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{base}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                raw = response.read().decode("utf-8") or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:400]
            except Exception:  # noqa: BLE001 - detail is best-effort
                pass
            logger.error(
                "Registry call %s failed with HTTP %s: %s", path, exc.code, detail
            )
            raise RegistryOperationFailedError(
                "The model registry rejected the operation.",
                details={"status": exc.code},
            ) from exc
        except urllib.error.URLError as exc:
            raise MLflowUnavailableError(
                "The model tracking service could not be reached.",
                details={"reason": "connection failed"},
            ) from exc
        except json.JSONDecodeError as exc:
            raise RegistryOperationFailedError(
                "The model registry returned an unreadable response."
            ) from exc

    # -- helpers -----------------------------------------------------------

    def _require_version(self, model: str, version: str) -> ResolvedModelVersion:
        """
        Resolve an exact version, bypassing the Production-stage requirement.

        A promotion necessarily targets a version that is *not* in Production,
        so the normal resolver -- which refuses anything unpromoted -- cannot be
        used here.
        """
        normalised = (model or "").strip().lower()
        if normalised not in self.registry.known_models():
            raise ModelNotFoundError(
                f"Unknown model '{model}'.",
                field="model",
                details={"available_models": list(self.registry.known_models())},
            )

        versions = self.registry.list_versions(normalised)
        for item in versions:
            if item.version == str(version):
                return item

        raise ModelNotFoundError(
            f"Version {version} of '{normalised}' was not found.",
            field="version",
            details={
                "model": normalised,
                "available_versions": [item.version for item in versions],
            },
        )

    def version_detail(self, model: str, version: str) -> ResolvedModelVersion:
        """One exact version, whatever its stage."""
        return self._require_version(model, version)

    def lineage(self, model: str, version: str) -> Dict[str, Any]:
        """
        Where a version came from.

        Everything needed to reproduce it: the training run, the dataset
        identity, and the stage it occupies. Artifact *paths* are deliberately
        absent -- an object-store location is not something a browser should be
        handed.
        """
        resolved = self._require_version(model, version)
        return {
            "model": resolved.model,
            "registered_model_name": resolved.registered_model_name,
            "version": resolved.version,
            "stage": resolved.stage,
            "run_id": resolved.run_id,
            "training_date": resolved.training_date,
            "dataset": resolved.lineage(),
            "metrics": resolved.metrics,
        }


__all__ = [
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "STAGE_ARCHIVED",
    "STAGE_NONE",
    "STAGE_PRODUCTION",
    "STAGE_STAGING",
    "MetricDelta",
    "MutationResult",
    "RegistryAction",
    "RegistryAdminService",
    "VersionComparison",
]
