"""Tests for feature schema loading and scenario capability detection."""

import json
import os
import sys
from typing import Dict, List, Optional

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from api.schemas.scenario import ScenarioType  # noqa: E402
from api.schemas.scenario_capabilities import (  # noqa: E402
    FeatureSchema,
    RequirementMode,
    ScenarioCapabilitiesResponse,
    ScenarioCapabilityStatus,
)
from api.services.feature_schema_service import (  # noqa: E402
    DEFAULT_FEATURE_SCHEMA_ARTIFACT,
    FEATURE_SCHEMA_MISSING_MESSAGE,
    FeatureSchemaError,
    FeatureSchemaErrorCode,
    FeatureSchemaService,
)
from api.services.scenario_capability_service import (  # noqa: E402
    SCENARIO_FEATURE_REQUIREMENTS,
    ScenarioCapabilityService,
)

ALL_FEATURES = [
    "lag_1",
    "lag_7",
    "rolling_mean_7",
    "is_promotion",
    "promotion_intensity",
    "customer_traffic",
    "is_holiday",
    "is_closed",
    "price",
]


class FakeArtifactClient:
    """In-memory ArtifactClient: maps model version -> run id -> artifact files."""

    def __init__(
        self,
        artifacts: Optional[Dict[str, Dict[str, str]]] = None,
        unknown_versions: Optional[List[str]] = None,
    ) -> None:
        # {model_version: {artifact_path: local_file_path}}
        self.artifacts = artifacts or {}
        self.unknown_versions = unknown_versions or []
        self.resolve_calls: List[str] = []
        self.download_calls: List[str] = []

    def resolve_run_id(self, model_version: str) -> str:
        self.resolve_calls.append(model_version)
        if model_version in self.unknown_versions:
            raise FeatureSchemaError(
                FeatureSchemaErrorCode.MODEL_VERSION_NOT_FOUND,
                f"Model version '{model_version}' was not found in the registry.",
                field="model_version",
                details={"model_version": model_version},
            )
        return f"run_{model_version}"

    def download_artifact(self, run_id: str, artifact_path: str) -> Optional[str]:
        self.download_calls.append(f"{run_id}:{artifact_path}")
        version = run_id.removeprefix("run_")
        return self.artifacts.get(version, {}).get(artifact_path)


def write_schema_artifact(
    tmp_path,
    features: List[str],
    model_version: str = "14",
    schema_version: str = "v1",
    filename: str = "feature_schema.json",
) -> str:
    """Write a feature_schema.json artifact and return its path."""
    payload = {
        "schema_version": schema_version,
        "model_version": model_version,
        "features": features,
    }
    path = tmp_path / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def build_schema_service(
    tmp_path,
    features: Optional[List[str]] = None,
    model_version: str = "14",
    artifacts: Optional[Dict[str, Dict[str, str]]] = None,
) -> FeatureSchemaService:
    """FeatureSchemaService backed by a fake artifact client."""
    if artifacts is None:
        artifact_file = write_schema_artifact(
            tmp_path,
            features if features is not None else ALL_FEATURES,
            model_version=model_version,
        )
        artifacts = {model_version: {DEFAULT_FEATURE_SCHEMA_ARTIFACT: artifact_file}}
    return FeatureSchemaService(artifact_client=FakeArtifactClient(artifacts=artifacts))


def build_capability_service(
    tmp_path,
    features: Optional[List[str]] = None,
    model_version: str = "14",
    **kwargs,
) -> ScenarioCapabilityService:
    """ScenarioCapabilityService over a fake feature schema."""
    return ScenarioCapabilityService(
        feature_schema_service=build_schema_service(
            tmp_path, features=features, model_version=model_version
        ),
        **kwargs,
    )


def status_map(response: ScenarioCapabilitiesResponse) -> Dict[ScenarioType, ScenarioCapabilityStatus]:
    return {capability.scenario_type: capability for capability in response.capabilities}


# --------------------------------------------------------------------------
# FeatureSchemaService
# --------------------------------------------------------------------------


def test_feature_schema_loads_from_artifact(tmp_path):
    service = build_schema_service(tmp_path)

    schema = service.get_feature_schema("14")

    assert isinstance(schema, FeatureSchema)
    assert schema.schema_version == "v1"
    assert schema.model_version == "14"
    assert schema.features == ALL_FEATURES
    assert schema.run_id == "run_14"
    assert schema.has_feature("PRICE")  # case-insensitive
    assert not schema.has_feature("store_event")


def test_feature_schema_is_cached_per_version(tmp_path):
    service = build_schema_service(tmp_path)

    first = service.get_feature_schema("14")
    second = service.get_feature_schema("14")

    assert first is second
    assert len(service.artifact_client.download_calls) == 1

    service.clear_cache()
    service.get_feature_schema("14")
    assert len(service.artifact_client.download_calls) == 2


def test_missing_feature_schema_raises_clear_error(tmp_path):
    # Run resolves, but the run holds no feature_schema.json artifact.
    service = FeatureSchemaService(artifact_client=FakeArtifactClient(artifacts={"14": {}}))

    with pytest.raises(FeatureSchemaError) as exc_info:
        service.get_feature_schema("14")

    error = exc_info.value
    assert error.error_code == "FEATURE_SCHEMA_MISSING"
    assert error.error_code == FeatureSchemaErrorCode.FEATURE_SCHEMA_MISSING.value
    assert error.message == FEATURE_SCHEMA_MISSING_MESSAGE
    assert error.message == "Feature schema is not available for this model version."
    assert error.field == "model_version"
    assert error.details["model_version"] == "14"
    assert error.details["run_id"] == "run_14"
    assert error.details["artifact_path"] == DEFAULT_FEATURE_SCHEMA_ARTIFACT


def test_missing_feature_schema_error_serialises(tmp_path):
    service = FeatureSchemaService(artifact_client=FakeArtifactClient(artifacts={"14": {}}))

    with pytest.raises(FeatureSchemaError) as exc_info:
        service.get_feature_schema("14")

    response = exc_info.value.to_response()
    assert response.error_code == "FEATURE_SCHEMA_MISSING"
    assert response.message == FEATURE_SCHEMA_MISSING_MESSAGE
    assert exc_info.value.to_dict() == response.model_dump()


def test_missing_schema_is_not_cached(tmp_path):
    artifacts: Dict[str, Dict[str, str]] = {"14": {}}
    service = FeatureSchemaService(artifact_client=FakeArtifactClient(artifacts=artifacts))

    with pytest.raises(FeatureSchemaError):
        service.get_feature_schema("14")

    # Training later publishes the artifact; no cache flush should be needed.
    artifacts["14"][DEFAULT_FEATURE_SCHEMA_ARTIFACT] = write_schema_artifact(
        tmp_path, ALL_FEATURES
    )
    assert service.get_feature_schema("14").features == ALL_FEATURES


def test_unknown_model_version_raises_not_found():
    service = FeatureSchemaService(
        artifact_client=FakeArtifactClient(artifacts={}, unknown_versions=["999"])
    )

    with pytest.raises(FeatureSchemaError) as exc_info:
        service.get_feature_schema("999")
    assert exc_info.value.error_code == "MODEL_VERSION_NOT_FOUND"


def test_malformed_json_raises_invalid(tmp_path):
    path = tmp_path / "feature_schema.json"
    path.write_text("{not json", encoding="utf-8")
    service = FeatureSchemaService(
        artifact_client=FakeArtifactClient(
            artifacts={"14": {DEFAULT_FEATURE_SCHEMA_ARTIFACT: str(path)}}
        )
    )

    with pytest.raises(FeatureSchemaError) as exc_info:
        service.get_feature_schema("14")
    assert exc_info.value.error_code == "FEATURE_SCHEMA_INVALID"


def test_empty_feature_list_raises_invalid(tmp_path):
    path = tmp_path / "feature_schema.json"
    path.write_text(
        json.dumps({"schema_version": "v1", "model_version": "14", "features": []}),
        encoding="utf-8",
    )
    service = FeatureSchemaService(
        artifact_client=FakeArtifactClient(
            artifacts={"14": {DEFAULT_FEATURE_SCHEMA_ARTIFACT: str(path)}}
        )
    )

    with pytest.raises(FeatureSchemaError) as exc_info:
        service.get_feature_schema("14")
    assert exc_info.value.error_code == "FEATURE_SCHEMA_INVALID"
    assert exc_info.value.details["validation_errors"]


def test_run_id_and_model_version_are_backfilled(tmp_path):
    path = tmp_path / "feature_schema.json"
    path.write_text(json.dumps({"features": ["lag_1"]}), encoding="utf-8")
    service = FeatureSchemaService(
        artifact_client=FakeArtifactClient(
            artifacts={"7": {DEFAULT_FEATURE_SCHEMA_ARTIFACT: str(path)}}
        )
    )

    schema = service.get_feature_schema("7")
    assert schema.model_version == "7"
    assert schema.run_id == "run_7"


# --------------------------------------------------------------------------
# Model with all features
# --------------------------------------------------------------------------


def test_model_with_all_features_supports_every_scenario(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)

    response = service.get_capabilities("14")

    assert isinstance(response, ScenarioCapabilitiesResponse)
    assert response.model_version == "14"
    assert response.run_id == "run_14"
    assert response.feature_count == len(ALL_FEATURES)
    assert response.warnings == []
    assert len(response.capabilities) == len(ScenarioType)

    statuses = status_map(response)
    for scenario_type in ScenarioType:
        assert statuses[scenario_type].supported, scenario_type

    assert statuses[ScenarioType.PROMOTION].available_features == [
        "is_promotion",
        "promotion_intensity",
    ]
    assert statuses[ScenarioType.PROMOTION].missing_features == []
    assert statuses[ScenarioType.PROMOTION].reason is None
    assert statuses[ScenarioType.TRAFFIC].available_features == ["customer_traffic"]
    assert statuses[ScenarioType.CLOSURE].available_features == ["is_closed"]
    assert statuses[ScenarioType.HOLIDAY].available_features == ["is_holiday"]
    assert statuses[ScenarioType.PRICE].available_features == ["price"]

    assert set(response.supported_scenario_types) == set(ScenarioType)
    assert response.is_supported(ScenarioType.PROMOTION)


def test_required_features_are_reported_for_every_type(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)
    statuses = status_map(service.get_capabilities("14"))

    for scenario_type, requirement in SCENARIO_FEATURE_REQUIREMENTS.items():
        if scenario_type is ScenarioType.CUSTOM:
            continue
        assert statuses[scenario_type].required_features == list(requirement.candidates)
        assert statuses[scenario_type].requirement_mode is RequirementMode.ANY


# --------------------------------------------------------------------------
# Model without promotion features
# --------------------------------------------------------------------------


def test_model_without_promotion_features_is_unsupported(tmp_path):
    """
    A schema carrying no promotion signal under any alias is unsupported.

    The reported `missing_features` is the full alias list, in preference
    order, so the message names every column that would satisfy the
    requirement rather than only one of them.
    """
    features = [
        name
        for name in ALL_FEATURES
        if name not in ("has_promotion", "is_promotion", "promotion_intensity")
    ]
    service = build_capability_service(tmp_path, features=features)
    response = service.get_capabilities("14")
    promotion = status_map(response)[ScenarioType.PROMOTION]

    assert promotion.supported is False
    assert promotion.available_features == []
    assert promotion.missing_features == [
        "has_promotion",
        "is_promotion",
        "promotion_intensity",
        "discount_depth",
    ]
    assert "was not trained with any of" in (promotion.reason or "")


def test_promotion_is_supported_via_the_has_promotion_alias(tmp_path):
    """
    `has_promotion` satisfies the promotion requirement.

    This is the name the training pipeline actually produces. Requiring
    `is_promotion` alone previously reported promotion scenarios as
    unsupported against a model that does carry the signal.
    """
    features = [
        name
        for name in ALL_FEATURES
        if name not in ("is_promotion", "promotion_intensity", "discount_depth")
    ] + ["has_promotion"]
    service = build_capability_service(tmp_path, features=features)
    promotion = status_map(service.get_capabilities("14"))[ScenarioType.PROMOTION]

    assert promotion.supported is True
    assert promotion.available_features == ["has_promotion"]
    assert promotion.missing_features == []

def test_promotion_supported_by_any_single_alias(tmp_path):
    service = build_capability_service(tmp_path, features=["lag_1", "discount_depth"])

    promotion = status_map(service.get_capabilities("14"))[ScenarioType.PROMOTION]

    assert promotion.supported is True
    assert promotion.available_features == ["discount_depth"]
    assert promotion.missing_features == []


# --------------------------------------------------------------------------
# Model without traffic features
# --------------------------------------------------------------------------


def test_model_without_traffic_features_is_unsupported(tmp_path):
    features = [f for f in ALL_FEATURES if f != "customer_traffic"]
    service = build_capability_service(tmp_path, features=features)

    statuses = status_map(service.get_capabilities("14"))
    traffic = statuses[ScenarioType.TRAFFIC]

    assert traffic.supported is False
    assert traffic.required_features == ["customer_traffic"]
    assert traffic.available_features == []
    assert traffic.missing_features == ["customer_traffic"]
    assert "customer_traffic" in traffic.reason

    assert statuses[ScenarioType.PROMOTION].supported is True


def test_closure_and_holiday_aliases_are_honoured(tmp_path):
    service = build_capability_service(
        tmp_path, features=["lag_1", "store_event", "holiday_proximity", "unit_price"]
    )

    statuses = status_map(service.get_capabilities("14"))

    assert statuses[ScenarioType.CLOSURE].supported is True
    assert statuses[ScenarioType.CLOSURE].available_features == ["store_event"]
    assert statuses[ScenarioType.HOLIDAY].supported is True
    assert statuses[ScenarioType.HOLIDAY].available_features == ["holiday_proximity"]
    assert statuses[ScenarioType.PRICE].supported is True
    assert statuses[ScenarioType.PRICE].available_features == ["unit_price"]
    assert statuses[ScenarioType.TRAFFIC].supported is False


def test_lag_only_model_supports_no_lever_scenarios(tmp_path):
    service = build_capability_service(tmp_path, features=["lag_1", "lag_7", "rolling_mean_7"])

    response = service.get_capabilities("14")
    statuses = status_map(response)

    for scenario_type in (
        ScenarioType.PROMOTION,
        ScenarioType.TRAFFIC,
        ScenarioType.CLOSURE,
        ScenarioType.HOLIDAY,
        ScenarioType.PRICE,
    ):
        assert statuses[scenario_type].supported is False
    # `custom` stays available: its columns are resolved per request.
    assert statuses[ScenarioType.CUSTOM].supported is True
    assert response.warnings == []


# --------------------------------------------------------------------------
# Missing feature schema
# --------------------------------------------------------------------------


def test_capabilities_propagate_missing_feature_schema():
    service = ScenarioCapabilityService(
        feature_schema_service=FeatureSchemaService(
            artifact_client=FakeArtifactClient(artifacts={"14": {}})
        )
    )

    with pytest.raises(FeatureSchemaError) as exc_info:
        service.get_capabilities("14")

    error = exc_info.value
    assert error.error_code == "FEATURE_SCHEMA_MISSING"
    assert error.message == "Feature schema is not available for this model version."


# --------------------------------------------------------------------------
# Custom overrides
# --------------------------------------------------------------------------


def test_custom_override_with_unsupported_column_is_unsupported(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)

    status = service.check_custom_overrides("14", ["weather_index"])

    assert status.scenario_type is ScenarioType.CUSTOM
    assert status.supported is False
    assert status.required_features == ["weather_index"]
    assert status.available_features == []
    assert status.missing_features == ["weather_index"]
    assert "weather_index" in status.reason


def test_custom_override_with_known_column_is_supported(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)

    status = service.check_custom_overrides("14", ["customer_traffic"])

    assert status.supported is True
    assert status.available_features == ["customer_traffic"]
    assert status.missing_features == []
    assert status.reason is None


def test_custom_override_mixed_columns_reports_the_gap(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)

    status = service.check_custom_overrides("14", ["price", "weather_index"])

    # At least one requested column exists, so the scenario is supported, but the
    # unknown column is still reported so the caller can surface it.
    assert status.supported is True
    assert status.available_features == ["price"]
    assert status.missing_features == ["weather_index"]


def test_custom_override_empty_request_is_unsupported(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)

    status = service.check_custom_overrides("14", [])

    assert status.supported is False
    assert "at least one override column" in status.reason


def test_custom_capability_without_requested_overrides(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)

    status = status_map(service.get_capabilities("14"))[ScenarioType.CUSTOM]

    assert status.supported is True
    assert status.required_features == []
    assert "per request" in status.reason


def test_requested_overrides_flow_through_get_capabilities(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)

    response = service.get_capabilities("14", requested_overrides=["weather_index"])

    assert response.is_supported(ScenarioType.PROMOTION)
    assert not response.is_supported(ScenarioType.CUSTOM)


# --------------------------------------------------------------------------
# Deployment-level gating
# --------------------------------------------------------------------------


def test_disabled_scenario_type_is_reported_unsupported(tmp_path):
    service = build_capability_service(
        tmp_path,
        features=ALL_FEATURES,
        enabled_scenario_types=[ScenarioType.PROMOTION, ScenarioType.TRAFFIC],
    )

    statuses = status_map(service.get_capabilities("14"))

    assert statuses[ScenarioType.PROMOTION].supported is True
    closure = statuses[ScenarioType.CLOSURE]
    assert closure.supported is False
    assert "not enabled" in closure.reason
    # The feature requirement is still advertised so the UI can explain the gap.
    assert closure.required_features == ["is_closed", "store_event"]


def test_capabilities_response_carries_request_limits(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)

    response = service.get_capabilities("14")

    assert response.min_history_rows == 7
    assert response.min_horizon_days == 1
    assert response.max_horizon_days == 90
    assert response.default_model_version == "latest"
    assert response.default_confidence_levels == [0.1, 0.5, 0.9]
    assert response.schema_version == "v1"


def test_capabilities_response_round_trips(tmp_path):
    service = build_capability_service(tmp_path, features=ALL_FEATURES)

    payload = service.get_capabilities("14").model_dump(mode="json")

    assert payload["model_version"] == "14"
    assert {cap["scenario_type"] for cap in payload["capabilities"]} == {
        item.value for item in ScenarioType
    }
    assert ScenarioCapabilitiesResponse.model_validate(payload).feature_count == len(
        ALL_FEATURES
    )


def test_evaluate_scenario_type_is_reusable_without_reloading(tmp_path):
    schema_service = build_schema_service(tmp_path, features=ALL_FEATURES)
    service = ScenarioCapabilityService(feature_schema_service=schema_service)
    schema = schema_service.get_feature_schema("14")

    before = len(schema_service.artifact_client.download_calls)
    statuses = [
        service.evaluate_scenario_type(scenario_type, schema) for scenario_type in ScenarioType
    ]

    assert all(status.supported for status in statuses)
    assert len(schema_service.artifact_client.download_calls) == before
