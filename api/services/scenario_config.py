"""
Scenario planning policy and capability registry.

Separates *what a scenario type means* (policy) from *what a request looks like*
(``api.schemas.scenario``). Request limits are imported from the schema module
rather than redefined, so there is a single source of truth for every bound.

Which scenario types are enabled is configurable via the ``SCENARIO_ENABLED_TYPES``
environment variable (comma-separated, e.g. ``promotion,traffic``) so deployments
can roll types out incrementally without a code change. Unset or empty falls back
to ``DEFAULT_ENABLED_SCENARIO_TYPES``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from api.schemas.scenario import (
    DEFAULT_CONFIDENCE_LEVELS,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_MODEL_VERSION,
    DISCOUNT_DEPTH_MAX,
    DISCOUNT_DEPTH_MIN,
    MAX_CLOSURE_DATES,
    MAX_HORIZON_DAYS,
    MIN_HISTORY_ROWS,
    MIN_HORIZON_DAYS,
    PromotionIntensity,
    ScenarioCapabilitiesResponse,
    ScenarioCapability,
    ScenarioType,
    TRAFFIC_MULTIPLIER_MAX,
    TRAFFIC_MULTIPLIER_MIN,
)

logger = logging.getLogger(__name__)

ENABLED_TYPES_ENV_VAR = "SCENARIO_ENABLED_TYPES"

DEFAULT_ENABLED_SCENARIO_TYPES: Tuple[ScenarioType, ...] = (
    ScenarioType.PROMOTION,
    ScenarioType.TRAFFIC,
    ScenarioType.CLOSURE,
    ScenarioType.HOLIDAY,
    ScenarioType.CUSTOM,
)

# Planning defaults, NOT fitted effects. A later scenario engine may replace
# these with coefficients learned from data; until then they are explicit,
# reviewable assumptions rather than numbers buried in the engine.
PROMOTION_INTENSITY_UPLIFT: Dict[PromotionIntensity, float] = {
    PromotionIntensity.NONE: 1.00,
    PromotionIntensity.LOW: 1.05,
    PromotionIntensity.MEDIUM: 1.15,
    PromotionIntensity.HIGH: 1.30,
}

CLOSURE_SALES_MULTIPLIER = 0.0
HOLIDAY_DEFAULT_UPLIFT = 1.10

# Per-type policy. ``required``/``optional`` name fields of ScenarioAssumptions;
# ``constraints`` is advertised verbatim to clients for form building.
_CAPABILITY_SPECS: Dict[ScenarioType, Dict[str, Any]] = {
    ScenarioType.PROMOTION: {
        "label": "Promotion",
        "description": (
            "Model a discount or promotional campaign over a date window and "
            "compare it against the unpromoted baseline."
        ),
        "required": ["start_date", "end_date"],
        "optional": ["promotion_intensity", "discount_depth", "duration_days"],
        "constraints": {
            "discount_depth_min": DISCOUNT_DEPTH_MIN,
            "discount_depth_max": DISCOUNT_DEPTH_MAX,
            "promotion_intensity_values": [item.value for item in PromotionIntensity],
            "promotion_intensity_uplift": {
                item.value: uplift for item, uplift in PROMOTION_INTENSITY_UPLIFT.items()
            },
        },
        "notes": (
            "Uplift factors are planning defaults, not fitted causal effects. "
            "Treat the output as a directional comparison."
        ),
    },
    ScenarioType.TRAFFIC: {
        "label": "Customer traffic",
        "description": "Scale expected customer traffic up or down over a date window.",
        "required": ["start_date", "end_date", "traffic_multiplier"],
        "optional": ["duration_days"],
        "constraints": {
            "traffic_multiplier_min": TRAFFIC_MULTIPLIER_MIN,
            "traffic_multiplier_max": TRAFFIC_MULTIPLIER_MAX,
        },
        "notes": None,
    },
    ScenarioType.CLOSURE: {
        "label": "Store closure",
        "description": "Zero out sales on specific closure dates and show the shortfall.",
        "required": ["closure_dates"],
        "optional": ["start_date", "end_date", "duration_days"],
        "constraints": {
            "max_closure_dates": MAX_CLOSURE_DATES,
            "closure_sales_multiplier": CLOSURE_SALES_MULTIPLIER,
            "closure_dates_must_not_precede_last_history_date": True,
        },
        "notes": "Closure dates must fall on or after the latest observed history date.",
    },
    ScenarioType.HOLIDAY: {
        "label": "Holiday period",
        "description": "Treat a date window as a holiday period and apply a holiday uplift.",
        "required": ["start_date", "end_date"],
        "optional": ["duration_days", "promotion_intensity"],
        "constraints": {"holiday_default_uplift": HOLIDAY_DEFAULT_UPLIFT},
        "notes": None,
    },
    ScenarioType.CUSTOM: {
        "label": "Custom overrides",
        "description": (
            "Supply arbitrary feature overrides for advanced what-if analysis."
        ),
        "required": ["custom_overrides"],
        "optional": ["start_date", "end_date", "duration_days"],
        "constraints": {"custom_overrides_value_types": ["float", "int", "str"]},
        "notes": "Override keys are validated against the model feature contract downstream.",
    },
}


def _parse_enabled_types(raw: str) -> Tuple[ScenarioType, ...]:
    """Parse a comma-separated scenario-type list, skipping unknown names."""
    parsed: List[ScenarioType] = []
    for token in raw.split(","):
        name = token.strip().lower()
        if not name:
            continue
        try:
            scenario_type = ScenarioType(name)
        except ValueError:
            logger.warning(
                "Ignoring unknown scenario type '%s' in %s", name, ENABLED_TYPES_ENV_VAR
            )
            continue
        if scenario_type not in parsed:
            parsed.append(scenario_type)
    return tuple(parsed)


def get_enabled_scenario_types() -> Tuple[ScenarioType, ...]:
    """Scenario types enabled for this deployment, in declaration order."""
    raw = os.getenv(ENABLED_TYPES_ENV_VAR, "")
    if raw.strip():
        parsed = _parse_enabled_types(raw)
        if parsed:
            return parsed
        logger.warning(
            "%s was set but contained no recognised scenario types; using defaults",
            ENABLED_TYPES_ENV_VAR,
        )
    return DEFAULT_ENABLED_SCENARIO_TYPES


def is_scenario_supported(
    scenario_type: ScenarioType,
    enabled_types: Optional[Tuple[ScenarioType, ...]] = None,
) -> bool:
    """Whether ``scenario_type`` is enabled."""
    enabled = get_enabled_scenario_types() if enabled_types is None else enabled_types
    return scenario_type in enabled


def get_scenario_capability(
    scenario_type: ScenarioType,
    enabled_types: Optional[Tuple[ScenarioType, ...]] = None,
) -> ScenarioCapability:
    """Capability descriptor for one scenario type."""
    spec = _CAPABILITY_SPECS[scenario_type]
    return ScenarioCapability(
        scenario_type=scenario_type,
        supported=is_scenario_supported(scenario_type, enabled_types),
        label=spec["label"],
        description=spec["description"],
        required_fields=list(spec["required"]),
        optional_fields=list(spec["optional"]),
        constraints=dict(spec["constraints"]),
        notes=spec["notes"],
    )


def get_scenario_capabilities(
    enabled_types: Optional[Tuple[ScenarioType, ...]] = None,
) -> List[ScenarioCapability]:
    """Capability descriptors for every known scenario type."""
    resolved = get_enabled_scenario_types() if enabled_types is None else enabled_types
    return [
        get_scenario_capability(scenario_type, resolved) for scenario_type in ScenarioType
    ]


def get_required_fields(scenario_type: ScenarioType) -> List[str]:
    """Assumption fields that must be populated for ``scenario_type``."""
    return list(_CAPABILITY_SPECS[scenario_type]["required"])


def get_promotion_uplift(intensity: PromotionIntensity) -> float:
    """Planning uplift multiplier for a promotion intensity."""
    return PROMOTION_INTENSITY_UPLIFT[intensity]


def get_capabilities_response(
    enabled_types: Optional[Tuple[ScenarioType, ...]] = None,
) -> ScenarioCapabilitiesResponse:
    """Full capabilities payload, including request limits."""
    return ScenarioCapabilitiesResponse(
        capabilities=get_scenario_capabilities(enabled_types),
        min_history_rows=MIN_HISTORY_ROWS,
        min_horizon_days=MIN_HORIZON_DAYS,
        max_horizon_days=MAX_HORIZON_DAYS,
        default_horizon_days=DEFAULT_HORIZON_DAYS,
        default_model_version=DEFAULT_MODEL_VERSION,
        default_confidence_levels=list(DEFAULT_CONFIDENCE_LEVELS),
    )
