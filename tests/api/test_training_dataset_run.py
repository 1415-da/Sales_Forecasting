"""
The dataset-driven DAG path, without Airflow, MLflow or MinIO.

Covers what the DAG must guarantee: it loads exactly the approved version
(and refuses anything else), it never generates or samples data, its quality
gate tests reloaded artifacts, its comparison refuses unfair matchups, it never
promotes, and its cleanup cannot touch stored datasets or artifacts.
"""

from __future__ import annotations

import ast
import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "include"))

from training import contract, dataset_run  # noqa: E402
from training.configs import DEPLOYMENT_POLICY, TRAINING_CONFIGS  # noqa: E402

DAG_FILE = os.path.join(ROOT, "dags", "sales_forecast_training.py")
DATASET_ID = "a" * 32


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def get(self, key):
        return self.objects[key]


def sealed(days=200):
    rows = [
        {"date": day.strftime("%Y-%m-%d"), "store_id": store, "sales": 1000.0 + i + n}
        for i, store in enumerate(("CA_1", "TX_1"))
        for n, day in enumerate(pd.date_range("2015-01-01", periods=days))
    ]
    result = contract.validate(
        contract.read_table(pd.DataFrame(rows).to_csv(index=False).encode(), "csv"),
        contract.ValidationPolicy(),
        currency="USD",
        source_type="real",
    )
    frame = result.frame
    store = MemoryStore()
    parquet = contract.to_parquet_bytes(frame)
    content_sha = contract.content_hash(frame)
    processed_key = f"datasets/{DATASET_ID}/processed/{content_sha}.parquet"
    store.objects[processed_key] = parquet
    manifest = {
        "dataset_id": DATASET_ID,
        "name": "Demo",
        "version": 1,
        "schema_version": contract.SCHEMA_VERSION,
        "target": "sales",
        "frequency": "D",
        "currency": "USD",
        "source_types": ["real"],
        "stores": ["CA_1", "TX_1"],
        "row_count": len(frame),
        "store_count": 2,
        "date_min": "2015-01-01",
        "date_max": frame["date"].max().date().isoformat(),
        "processed": {
            "object_key": processed_key,
            "object_sha256": contract.sha256_bytes(parquet),
            "content_sha256": content_sha,
        },
        "validation": {"policy": result.report["policy"]},
    }
    manifest_bytes = json.dumps(manifest).encode()
    store.objects[f"datasets/{DATASET_ID}/manifest.json"] = manifest_bytes
    conf = {
        "training_request_id": "b" * 32,
        "dataset_id": DATASET_ID,
        "dataset_version": 1,
        "manifest_uri": f"s3://training-datasets/datasets/{DATASET_ID}/manifest.json",
        "manifest_sha256": contract.sha256_bytes(manifest_bytes),
        "content_sha256": content_sha,
        "training_config_id": "standard_v1",
        "deployment_policy": DEPLOYMENT_POLICY,
    }
    return store, conf, frame


# -- loading -----------------------------------------------------------------


def test_the_exact_sealed_dataset_is_loaded_in_full():
    store, conf, frame = sealed()
    manifest, loaded = dataset_run.load_dataset(conf, store)
    assert len(loaded) == len(frame) == 400
    assert contract.content_hash(loaded) == conf["content_sha256"]
    assert manifest["name"] == "Demo"


@pytest.mark.parametrize("tamper", ["manifest", "processed", "conf_hash"])
def test_any_mismatch_with_the_approved_version_stops_the_run(tamper):
    store, conf, _ = sealed()
    if tamper == "manifest":
        key = f"datasets/{DATASET_ID}/manifest.json"
        store.objects[key] = store.objects[key].replace(b'"Demo"', b'"Other"')
    elif tamper == "processed":
        key = next(k for k in store.objects if "/processed/" in k)
        store.objects[key] = store.objects[key][:-8] + b"PAR1PAR1"
    else:
        conf["content_sha256"] = "0" * 64
    with pytest.raises(dataset_run.DatasetIntegrityError):
        dataset_run.load_dataset(conf, store)


def test_a_manifest_uri_outside_the_dataset_bucket_is_refused():
    store, conf, _ = sealed()
    conf["manifest_uri"] = "s3://mlflow-artifacts/datasets/x/manifest.json"
    with pytest.raises(ValueError):
        dataset_run.load_dataset(conf, store)


def test_revalidation_covers_every_record():
    store, conf, frame = sealed()
    manifest, loaded = dataset_run.load_dataset(conf, store)
    summary = dataset_run.revalidate(manifest, loaded)
    assert summary["rows"] == len(frame)
    assert summary["split"]["valid"]


def test_a_dataset_run_is_recognised_only_by_a_complete_conf():
    assert not dataset_run.is_dataset_run({})
    assert dataset_run.is_dataset_run({"training_request_id": "x"})
    _, conf, _ = sealed()
    with pytest.raises(ValueError):
        dataset_run.require_conf({**conf, "manifest_uri": ""})
    with pytest.raises(ValueError):
        dataset_run.require_conf({**conf, "deployment_policy": "auto_promote"})


def test_extract_bypasses_generation_and_reports_every_row(monkeypatch):
    from training import dag_tasks

    store, conf, frame = sealed()
    monkeypatch.setattr(dataset_run, "DatasetStore", lambda: store)
    result = dag_tasks.extract(conf)
    assert result["mode"] == "dataset"
    assert result["row_count"] == len(frame)
    assert result["lineage"]["data_source"] == "uploaded"
    assert result["lineage"]["regenerated"] is False
    source = open(os.path.join(ROOT, "include", "training", "dag_tasks.py")).read()
    assert "get_data_provider" not in source
    assert "max_sales_files" not in source


# -- quality gate ------------------------------------------------------------


def gate(**overrides):
    values = dict(
        model="ensemble",
        loaded_feature_cols=["store_id", "sales_lag_1"],
        trained_feature_cols=["store_id", "sales_lag_1"],
        predict=lambda: np.array([1.0, 2.0, 3.0]),
        in_memory_predictions=[1.0, 2.0, 3.0],
        supported_stores=["CA_1"],
        dataset_stores=["CA_1"],
        test_rows=3,
        min_test_rows=3,
        metrics={"rmse": 1.0, "mae": 1.0, "wape": 5.0},
    )
    values.update(overrides)
    return dataset_run.quality_gate(**values)


def failed(result):
    return {item["check"] for item in result["checks"] if not item["passed"]}


def test_a_compatible_bundle_passes_every_check():
    assert gate()["passed"]


@pytest.mark.parametrize(
    "overrides,check",
    [
        ({"load_error": "boom", "predict": None}, "artifacts_load"),
        ({"loaded_feature_cols": ["sales_lag_1", "store_id"]}, "feature_contract"),
        ({"loaded_feature_cols": ["store_id", "profit"], "trained_feature_cols": ["store_id", "profit"]}, "feature_contract"),
        ({"predict": lambda: np.array([1.0, np.nan, 3.0])}, "predictions_finite"),
        ({"predict": lambda: np.array([1.0, 2.0])}, "predictions_finite"),
        ({"predict": lambda: np.array([1.0, 2.0, 9.0])}, "predictions_finite"),
        ({"supported_stores": ["CA_1", "WI_1"]}, "store_support"),
        ({"min_test_rows": 100}, "evaluation_sample"),
        ({"metrics": {"rmse": 1.0, "mae": 1.0}}, "metrics_present"),
    ],
)
def test_each_gate_check_fails_on_its_own_defect(overrides, check):
    result = gate(**overrides)
    assert not result["passed"]
    assert check in failed(result)


# -- comparison --------------------------------------------------------------


def comparison(**overrides):
    features = pd.DataFrame({"store_id": ["CA_1", "CA_1", "WI_9"], "sales_lag_1": [1.0, 2.0, 3.0]})
    values = dict(
        champion={
            "model": "ensemble",
            "version": "2",
            "data_date_max": "2015-12-31",
            "feature_cols": ["store_id", "sales_lag_1"],
            "stores": ["CA_1"],
        },
        test_window=["2016-01-01", "2016-01-31"],
        test_features=features,
        actual=[100.0, 100.0, 100.0],
        candidate_predictions=[90.0, 110.0, 100.0],
        champion_predict=lambda rows: np.full(len(rows), 80.0),
    )
    values.update(overrides)
    return dataset_run.champion_comparison(**values)


def test_models_are_compared_on_identical_rows_only():
    result = comparison()
    assert result["comparable"]
    assert result["rows"] == 2 and result["rows_excluded"] == 1
    assert result["candidate_wape"] == pytest.approx(10.0)
    assert result["champion_wape"] == pytest.approx(20.0)


def test_a_champion_that_trained_on_the_test_window_is_not_comparable():
    result = comparison(champion={"model": "ensemble", "version": "2", "data_date_max": "2016-04-24", "feature_cols": ["store_id"], "stores": ["CA_1"]})
    assert not result["comparable"]
    assert "Not directly comparable" in result["reason"]
    assert "candidate_wape" not in result


def test_no_champion_or_missing_inputs_are_reported_not_scored():
    assert not comparison(champion=None)["comparable"]
    missing = comparison(champion={"model": "ensemble", "version": "2", "data_date_max": None, "feature_cols": ["customer_traffic"], "stores": ["CA_1"]})
    assert not missing["comparable"]


# -- configurations ----------------------------------------------------------


def test_the_api_offers_exactly_the_configurations_the_dag_resolves():
    from api.core.rbac import Permission, Role, has_permission

    assert set(TRAINING_CONFIGS) == {"standard_v1", "tuned_v1"}
    for config in TRAINING_CONFIGS.values():
        Permission(config.required_permission)  # a real permission
    assert has_permission(Role.ANALYST, Permission(TRAINING_CONFIGS["standard_v1"].required_permission))
    assert not has_permission(Role.ANALYST, Permission(TRAINING_CONFIGS["tuned_v1"].required_permission))


# -- the DAG file ------------------------------------------------------------


def _dag_function(name: str) -> str:
    source = open(DAG_FILE).read()
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, node)


def test_dataset_runs_never_promote():
    body = _dag_function("transition_to_production_task")
    dataset_branch = body.index('registration_result.get("mode") == "dataset"')
    promote_call = body.index("transition_model_stage")
    assert dataset_branch < promote_call
    assert '"skipped": "manual_review"' in body


def test_every_data_task_branches_before_touching_the_provider():
    extract = _dag_function("extract_data_task")
    assert extract.index("_dataset_conf()") < extract.index("get_data_provider")
    train = _dag_function("train_models_task")
    assert train.index('"dataset"') < train.index("max_sales_files")


def test_cleanup_cannot_remove_datasets_or_artifacts():
    source = open(DAG_FILE).read()
    command = source[source.index('task_id="cleanup"'):].split("bash_command=", 1)[1].split("\n", 1)[0]
    assert command.strip().startswith('"rm -f /tmp/performance_report.json')
    assert "datasets" not in command and "mlflow" not in command and "*" not in command
