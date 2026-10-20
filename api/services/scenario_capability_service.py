"""
Scenario capability detection.

Answers the question the frontend asks before rendering a what-if form: given the
feature schema of the selected model version, which scenario types can it
actually support?

A scenario type is supported only when BOTH hold:
  1. the type is enabled for this deployment (``scenario_config``), and
  2. the model's feature schema satisfies the type's feature requirement.

This service is deliberately free of FastAPI imports -- it takes a model version
and returns schema objects, so it is callable from a route handler, a test, or a
background job alike.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from api.schemas.scenario import DEFAULT_MODEL_VERSION, ScenarioType
from api.schemas.scenario_capabilities import (
    FeatureSchema,
    RequirementMode,
    ScenarioCapabilitiesResponse,
    ScenarioCapabilityStatus,
)
from api.services.feature_schema_service import FeatureSchemaService
from api.services.scenario_config import get_enabled_scenario_types

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureRequirement:
    """The model features a scenario type needs in order to be simulated."""

    scenario_type: ScenarioType
    candidates: Tuple[str, ...]
    mode: RequirementMode = RequirementMode.ANY
    description: str = ""

    def evaluate(self, schema: FeatureSchema) -> Tuple[bool, List[str], List[str]]:
        """Return ``(satisfied, available, missing)`` against a feature schema."""
        candidates = list(self.candidates)
        available = schema.present(candidates)
        absent = schema.absent(candidates)

        if self.mode is RequirementMode.ALL:
            return (not absent, available, absent)
        # ANY: satisfied by a single candidate; nothing is "blocking" if one exists.
        satisfied = bool(available)
        return (satisfied, available, [] if satisfied else absent)


# Feature requirements per scenario type. Candidate names are the aliases a
# trained feature schema may legitimately use for the same signal.
SCENARIO_FEATURE_REQUIREMENTS: Dict[ScenarioType, FeatureRequirement] = {
    ScenarioType.PROMOTION: FeatureRequirement(
        scenario_type=ScenarioType.PROMOTION,
        candidates=("is_promotion", "promotion_intensity", "discount_depth"),
        mode=RequirementMode.ANY,
        description="Needs a promotion signal the model was trained on.",
    ),
    ScenarioType.TRAFFIC: FeatureRequirement(
        scenario_type=ScenarioType.TRAFFIC,
        candidates=("customer_traffic",),
        mode=RequirementMode.ANY,
        description="Needs a customer traffic feature.",
    ),
    ScenarioType.CLOSURE: FeatureRequirement(
        scenario_type=ScenarioType.CLOSURE,
        candidates=("is_closed", "store_event"),
        mode=RequirementMode.ANY,
        description="Needs a closure or store-event feature.",
    ),
    ScenarioType.HOLIDAY: FeatureRequirement(
        scenario_type=ScenarioType.HOLIDAY,
        candidates=("is_holiday", "holiday_proximity"),
        mode=RequirementMode.ANY,
        description="Needs a holiday indicator or holiday proximity feature.",
    ),
    ScenarioType.PRICE: FeatureRequirement(
        scenario_type=ScenarioType.PRICE,
        candidates=("price", "unit_price", "discount"),
        mode=RequirementMode.ANY,
        description="Needs a price or discount feature.",
    ),
    ScenarioType.CUSTOM: FeatureRequirement(
        scenario_type=ScenarioType.CUSTOM,
        candidates=(),
        mode=RequirementMode.ANY,
        description="Needs at least one requested override column to exist in the schema.",
    ),
}

DISABLED_REASON_TEMPLATE = "Scenario type '{scenario_type}' is not enabled for this deployment."
MISSING_FEATURES_REASON_TEMPLATE = (
    "Model version {model_version} was not trained with any of: {candidates}."
)
CUSTOM_UNREQUESTED_REASON = (
    "Custom overrides are validated per request against the model feature schema."
)
CUSTOM_MISSING_REASON_TEMPLATE = (
    "None of the requested override columns exist in the feature schema for model "
    "version {model_version}: {requested}."
)


class ScenarioCapabilityService:
    """
    Detects which scenario types a model version supports.

    Dependencies are injectable so this can be tested with a fake feature schema
    service and no MLflow server.
    """

    def __init__(
        self,
        feature_schema_service: Optional[FeatureSchemaService] = None,
        enabled_scenario_types: Optional[Sequence[ScenarioType]] = None,
        requirements: Optional[Dict[ScenarioType, FeatureRequirement]] = None,
    ) -> None:
        self.feature_schema_service = feature_schema_service or FeatureSchemaService()
        self._enabled_scenario_types: Optional[Tuple[ScenarioType, ...]] = (
            tuple(enabled_scenario_types) if enabled_scenario_types is not None else None
        )
        self.requirements = dict(requirements or SCENARIO_FEATURE_REQUIREMENTS)

    @property
    def enabled_scenario_types(self) -> Tuple[ScenarioType, ...]:
        """Enabled types, resolved from config when not injected."""
        if self._enabled_scenario_types is not None:
            return self._enabled_scenario_types
        return get_enabled_scenario_types()

    def get_capabilities(
        self,
        model_version: str = DEFAULT_MODEL_VERSION,
        requested_overrides: Optional[Iterable[str]] = None,
    ) -> ScenarioCapabilitiesResponse:
        """
        Capabilities for every known scenario type under ``model_version``.

        ``requested_overrides`` is optional: when omitted the ``custom``
        capability reports that override columns are checked per request; when
        supplied those columns are resolved against the schema immediately.

        Propagates ``FeatureSchemaError`` (FEATURE_SCHEMA_MISSING and friends) so
        the caller can return a precise error instead of a misleading
        "nothing supported" payload.
        """
        schema = self.feature_schema_service.get_feature_schema(model_version)
        overrides = list(requested_overrides) if requested_overrides is not None else None

        capabilities = [
            self.evaluate_scenario_type(scenario_type, schema, overrides)
            for scenario_type in ScenarioType
        ]

        warnings: List[str] = []
        if not any(capability.supported for capability in capabilities):
            warnings.append(
                f"Model version {schema.model_version} supports no scenario types; "
                "its feature schema contains no scenario-controllable features."
            )

        return ScenarioCapabilitiesResponse(
            model_version=schema.model_version,
            feature_schema_version=schema.schema_version,
            run_id=schema.run_id,
            feature_count=len(schema.features),
            features=list(schema.features),
            capabilities=capabilities,
            warnings=warnings,
        )

    def get_capability(
        self,
        model_version: str,
        scenario_type: ScenarioType,
        requested_overrides: Optional[Iterable[str]] = None,
    ) -> ScenarioCapabilityStatus:
        """Capability status for a single scenario type."""
        schema = self.feature_schema_service.get_feature_schema(model_version)
        overrides = list(requested_overrides) if requested_overrides is not None else None
        return self.evaluate_scenario_type(scenario_type, schema, overrides)

    def check_custom_overrides(
        self,
        model_version: str,
        requested_overrides: Iterable[str],
    ) -> ScenarioCapabilityStatus:
        """Resolve specific custom override columns against the model schema."""
        return self.get_capability(
            model_version, ScenarioType.CUSTOM, requested_overrides=requested_overrides
        )

    def evaluate_scenario_type(
        self,
        scenario_type: ScenarioType,
        schema: FeatureSchema,
        requested_overrides: Optional[List[str]] = None,
    ) -> ScenarioCapabilityStatus:
        """
        Evaluate one scenario type against an already-loaded feature schema.

        Pure and side-effect free: useful when several types are checked against
        the same schema without re-reading the artifact.
        """
        if scenario_type not in self.enabled_scenario_types:
            return self._disabled_status(scenario_type)

        if scenario_type is ScenarioType.CUSTOM:
            return self._custom_status(schema, requested_overrides)

        requirement = self.requirements.get(scenario_type)
        if requirement is None:
            return ScenarioCapabilityStatus(
                scenario_type=scenario_type,
                supported=False,
                reason=f"No feature requirement is registered for '{scenario_type.value}'.",
            )

        satisfied, available, missing = requirement.evaluate(schema)
        reason = (
            None
            if satisfied
            else MISSING_FEATURES_REASON_TEMPLATE.format(
                model_version=schema.model_version,
                candidates=", ".join(requirement.candidates),
            )
        )
        return ScenarioCapabilityStatus(
            scenario_type=scenario_type,
            supported=satisfied,
            required_features=list(requirement.candidates),
            available_features=available,
            missing_features=missing,
            requirement_mode=requirement.mode,
            reason=reason,
        )

    def _disabled_status(self, scenario_type: ScenarioType) -> ScenarioCapabilityStatus:
        """Status for a type switched off by configuration."""
        requirement = self.requirements.get(scenario_type)
        return ScenarioCapabilityStatus(
            scenario_type=scenario_type,
            supported=False,
            required_features=list(requirement.candidates) if requirement else [],
            requirement_mode=requirement.mode if requirement else RequirementMode.ANY,
            reason=DISABLED_REASON_TEMPLATE.format(scenario_type=scenario_type.value),
        )

    def _custom_status(
        self,
        schema: FeatureSchema,
        requested_overrides: Optional[List[str]],
    ) -> ScenarioCapabilityStatus:
        """
        Custom scenarios need at least one requested override column to exist.

        With no columns requested the capability is advertised as available and
        the columns are resolved per request.
        """
        requirement = self.requirements[ScenarioType.CUSTOM]

        if requested_overrides is None:
            return ScenarioCapabilityStatus(
                scenario_type=ScenarioType.CUSTOM,
                supported=bool(schema.features),
                requirement_mode=requirement.mode,
                reason=CUSTOM_UNREQUESTED_REASON,
            )

        requested = [name for name in requested_overrides if name and name.strip()]
        available = schema.present(requested)
        missing = schema.absent(requested)
        supported = bool(available)

        if not requested:
            reason: Optional[str] = (
                "Custom scenarios require at least one override column."
            )
            supported = False
        elif supported:
            reason = None
        else:
            reason = CUSTOM_MISSING_REASON_TEMPLATE.format(
                model_version=schema.model_version, requested=", ".join(requested)
            )

        # Unlike the alias-based any-of requirements, requested override columns
        # are explicit user input, so the absent ones are reported even when the
        # request is satisfiable. `supported` is still driven by available_features.
        return ScenarioCapabilityStatus(
            scenario_type=ScenarioType.CUSTOM,
            supported=supported,
            required_features=requested,
            available_features=available,
            missing_features=missing,
            requirement_mode=requirement.mode,
            reason=reason,
        )
