"""
Task bodies for dataset-driven training runs.

Kept out of the DAG file so the DAG reads as orchestration only. Everything
here runs inside an Airflow worker (it imports the trainer, MLflow and boto3);
the decisions it applies live in ``training.dataset_run`` and
``training.contract``, which are tested on their own.

Each task reloads what it needs -- the dataset from its manifest, the model
from the MLflow run -- rather than trusting another task's memory. That is
slower by seconds and it is the point: the quality gate tests the artifacts a
server would load, not the objects the trainer happened to hold.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from training import dataset_run
from training.configs import MODEL_FAMILIES, get_config

logger = logging.getLogger(__name__)

EVALUATION_DIR = "evaluation"
HOLDOUT_ARTIFACT = f"{EVALUATION_DIR}/holdout_predictions.csv"
GROUP_COLS = ["store_id"]


# ---------------------------------------------------------------------------
# MLflow and artifact helpers
# ---------------------------------------------------------------------------


def _mlflow_client():
    import mlflow
    from mlflow.tracking import MlflowClient

    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://mlflow:5001"))
    return MlflowClient()


def _s3():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://minio:9000"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


def _artifact_bytes(artifact_uri: str, relative: str) -> bytes:
    bucket, key = dataset_run.artifact_key(artifact_uri, relative)
    return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()


def _joblib_artifact(artifact_uri: str, relative: str) -> Any:
    import joblib

    with tempfile.NamedTemporaryFile(suffix=".pkl") as handle:
        handle.write(_artifact_bytes(artifact_uri, relative))
        handle.flush()
        return joblib.load(handle.name)


def _log_json(run_id: str, payload: Dict[str, Any], artifact_file: str) -> None:
    client = _mlflow_client()
    with tempfile.TemporaryDirectory() as staging:
        path = os.path.join(staging, os.path.basename(artifact_file))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        client.log_artifact(run_id, path, os.path.dirname(artifact_file) or None)


def _load_bundle(artifact_uri: str, model: str) -> Dict[str, Any]:
    scalers = _joblib_artifact(artifact_uri, "scalers.pkl")
    scaler = scalers.get("standard") if isinstance(scalers, dict) else scalers
    return {
        "feature_cols": [str(c) for c in _joblib_artifact(artifact_uri, "feature_cols.pkl")],
        "scaler": scaler,
        "encoders": _joblib_artifact(artifact_uri, "encoders.pkl") or {},
        "model": _joblib_artifact(artifact_uri, f"models/{model}/{model}_model.pkl"),
    }


def _matrix(bundle: Dict[str, Any], features: pd.DataFrame) -> pd.DataFrame:
    """Encode and scale exactly as ``ModelTrainer.preprocess_features`` did."""
    cols = bundle["feature_cols"]
    frame = features[cols].copy()
    for column, encoder in (bundle["encoders"] or {}).items():
        if column in frame.columns:
            frame[column] = encoder.transform(frame[column].astype(str))
    values = bundle["scaler"].transform(frame) if bundle["scaler"] is not None else frame.values
    return pd.DataFrame(values, columns=cols, index=frame.index)


def _test_features(conf: Dict[str, Any], split: Dict[str, Any]):
    """Rebuild the test window's feature rows from the stored dataset."""
    from ml_models.train_models import ModelTrainer

    manifest, frame = dataset_run.load_dataset(conf)
    trainer = ModelTrainer()
    _, _, test_df = trainer.prepare_data(
        dataset_run.training_frame(frame),
        target_col="sales",
        date_col="date",
        group_cols=GROUP_COLS,
        categorical_cols=["store_id"],
        date_windows=split,
    )
    return manifest, test_df


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def extract(conf: Dict[str, Any]) -> Dict[str, Any]:
    conf = dataset_run.require_conf(conf)
    manifest, frame = dataset_run.load_dataset(conf)
    print(
        f"Dataset {manifest['name']} v{manifest['version']} ({conf['dataset_id']}): "
        f"{len(frame)} store-days, {frame['store_id'].nunique()} stores, "
        f"{manifest['date_min']} to {manifest['date_max']}"
    )
    print(f"Content sha256 verified: {manifest['processed']['content_sha256']}")
    print("Synthetic generation and the M5 provider are bypassed for this run.")
    return {
        "mode": "dataset",
        "conf": conf,
        "lineage": dataset_run.lineage_params(manifest, conf),
        "row_count": int(len(frame)),
    }


def validate(extract_result: Dict[str, Any]) -> Dict[str, Any]:
    conf = extract_result["conf"]
    manifest, frame = dataset_run.load_dataset(conf)
    summary = dataset_run.revalidate(manifest, frame)
    print(
        f"Full dataset re-validated: {summary['rows']} rows, {summary['stores']} stores, "
        f"{summary['dates']} dates. Split: {json.dumps(summary['split'])}"
    )
    return {"mode": "dataset", "total_rows": summary["rows"], **summary}


def train(extract_result: Dict[str, Any], validation_summary: Dict[str, Any]) -> Dict[str, Any]:
    from ml_models.train_models import ModelTrainer

    conf = extract_result["conf"]
    config = get_config(conf["training_config_id"])
    manifest, frame = dataset_run.load_dataset(conf)
    daily = dataset_run.training_frame(frame)
    split = validation_summary["split"]
    print(f"Training {config.label} on {len(daily)} store-days; split {json.dumps(split)}")

    trainer = ModelTrainer()
    trainer.config["training"]["optuna_trials"] = config.optuna_trials
    train_df, val_df, test_df = trainer.prepare_data(
        daily,
        target_col="sales",
        date_col="date",
        group_cols=GROUP_COLS,
        categorical_cols=["store_id"],
        date_windows=split,
    )
    print(f"Train {train_df.shape}, validation {val_df.shape}, test {test_df.shape}")
    results = trainer.train_all_models(
        train_df,
        val_df,
        test_df,
        target_col="sales",
        use_optuna=config.use_optuna,
        dataset_lineage=dataset_run.lineage_params(manifest, conf),
        run_tags=dataset_run.run_tags(manifest, conf),
    )
    run_id = trainer.current_run_id

    predictions = {name: results[name]["predictions"] for name in MODEL_FAMILIES if name in results}
    table = dataset_run.holdout_table(test_df, predictions)
    client = _mlflow_client()
    with tempfile.TemporaryDirectory() as staging:
        path = os.path.join(staging, "holdout_predictions.csv")
        table.to_csv(path, index=False)
        client.log_artifact(run_id, path, EVALUATION_DIR)

    ensemble = trainer.models.get("ensemble")
    metrics = {name: results[name]["metrics"] for name in MODEL_FAMILIES if name in results}
    return {
        "mode": "dataset",
        "conf": conf,
        "mlflow_run_id": run_id,
        "training_results": {name: {"metrics": values} for name, values in metrics.items()},
        "split": split,
        "test_rows": int(len(test_df)),
        "feature_cols": list(trainer.feature_cols),
        "ensemble_weights": dict(getattr(ensemble, "weights", {}) or {}),
        "data_lineage": extract_result.get("lineage"),
    }


def tag_versions(registration: Dict[str, Any], training: Dict[str, Any]) -> None:
    """Mark each registered candidate with its request and review policy."""
    from utils.mlflow_utils import MLflowManager

    conf = training["conf"]
    registry = MLflowManager().registry_name
    client = _mlflow_client()
    for model, version in (registration.get("versions") or {}).items():
        name = f"{registry}_{model}"
        for key, value in {
            "training_request_id": conf["training_request_id"],
            "dataset_id": conf["dataset_id"],
            "dataset_version": str(conf["dataset_version"]),
            "deployment_policy": conf["deployment_policy"],
            "candidate": "true",
        }.items():
            client.set_model_version_tag(name, str(version), key, value)
        print(f"Registered candidate {name} v{version} (stage None; awaiting review)")


def quality_gate(training: Dict[str, Any], registration: Dict[str, Any]) -> Dict[str, Any]:
    conf = training["conf"]
    config = get_config(conf["training_config_id"])
    client = _mlflow_client()
    artifact_uri = client.get_run(training["mlflow_run_id"]).info.artifact_uri
    manifest, test_df = _test_features(conf, training["split"])
    holdout = pd.read_csv(io.BytesIO(_artifact_bytes(artifact_uri, HOLDOUT_ARTIFACT)))
    dataset_stores = sorted(manifest["stores"])

    results: Dict[str, Any] = {}
    for model in MODEL_FAMILIES:
        if model not in (registration.get("versions") or {}):
            continue
        load_error: Optional[str] = None
        bundle: Optional[Dict[str, Any]] = None
        try:
            bundle = _load_bundle(artifact_uri, model)
        except Exception as exc:  # noqa: BLE001 - reported as a failed check
            load_error = f"Artifacts could not be reloaded: {type(exc).__name__}: {exc}"
        encoder = (bundle or {}).get("encoders", {}).get("store_id")
        gate = dataset_run.quality_gate(
            model=model,
            loaded_feature_cols=bundle["feature_cols"] if bundle else None,
            trained_feature_cols=training["feature_cols"],
            predict=(lambda b=bundle: b["model"].predict(_matrix(b, test_df))) if bundle else None,
            in_memory_predictions=holdout[model].to_numpy(),
            supported_stores=[str(v) for v in getattr(encoder, "classes_", [])],
            dataset_stores=dataset_stores,
            test_rows=int(len(test_df)),
            min_test_rows=config.min_test_rows,
            metrics=training["training_results"][model]["metrics"],
            load_error=load_error,
        )
        results[model] = gate
        version = registration["versions"][model]
        from utils.mlflow_utils import MLflowManager

        client.set_model_version_tag(
            f"{MLflowManager().registry_name}_{model}", str(version), "quality_gate", "passed" if gate["passed"] else "failed"
        )
        print(f"Quality gate {model}: {'PASSED' if gate['passed'] else 'FAILED'}")
        for item in gate["checks"]:
            print(f"  [{'x' if item['passed'] else ' '}] {item['check']}: {item['detail']}")
    _log_json(training["mlflow_run_id"], results, f"{EVALUATION_DIR}/quality_gate.json")
    return results


def _champion(model: str) -> Optional[Dict[str, Any]]:
    from utils.mlflow_utils import MLflowManager

    client = _mlflow_client()
    name = f"{MLflowManager().registry_name}_{model}"
    try:
        versions = client.search_model_versions(f"name='{name}'")
    except Exception:  # noqa: BLE001 - no registered model yet
        return None
    production = [v for v in versions if v.current_stage == "Production"]
    if not production:
        return None
    version = max(production, key=lambda v: int(v.version))
    run = client.get_run(version.run_id)
    params = run.data.params
    return {
        "model": model,
        "version": str(version.version),
        "run_id": version.run_id,
        "artifact_uri": run.info.artifact_uri,
        "data_date_max": params.get("data.date_max") or params.get("data.date_range_end"),
    }


def compare(training: Dict[str, Any]) -> Dict[str, Any]:
    conf = training["conf"]
    client = _mlflow_client()
    artifact_uri = client.get_run(training["mlflow_run_id"]).info.artifact_uri
    _, test_df = _test_features(conf, training["split"])
    holdout = pd.read_csv(io.BytesIO(_artifact_bytes(artifact_uri, HOLDOUT_ARTIFACT)))
    test_window = training["split"]["test"]

    comparisons: Dict[str, Any] = {}
    for model in MODEL_FAMILIES:
        champion = _champion(model)
        predict = None
        if champion:
            try:
                bundle = _load_bundle(champion["artifact_uri"], model)
                encoder = bundle["encoders"].get("store_id")
                champion["feature_cols"] = bundle["feature_cols"]
                champion["stores"] = [str(v) for v in getattr(encoder, "classes_", [])]
                predict = lambda rows, b=bundle: b["model"].predict(_matrix(b, rows))  # noqa: E731
            except Exception as exc:  # noqa: BLE001
                logger.warning("Champion %s could not be loaded: %s", model, exc)
        result = dataset_run.champion_comparison(
            champion=champion,
            test_window=test_window,
            test_features=test_df,
            actual=holdout["actual"].to_numpy(),
            candidate_predictions=holdout[model].to_numpy(),
            champion_predict=predict,
        )
        comparisons[model] = result
        print(f"{model}: {result['reason']}")
    _log_json(training["mlflow_run_id"], comparisons, f"{EVALUATION_DIR}/champion_comparison.json")
    return comparisons


def summarize(
    training: Dict[str, Any],
    registration: Dict[str, Any],
    gate: Dict[str, Any],
    comparison: Dict[str, Any],
) -> Dict[str, Any]:
    """Log the bundle contract and the evaluation report; hand the API a summary."""
    conf = training["conf"]
    manifest, frame = dataset_run.load_dataset(conf)
    client = _mlflow_client()
    artifact_uri = client.get_run(training["mlflow_run_id"]).info.artifact_uri
    holdout = pd.read_csv(io.BytesIO(_artifact_bytes(artifact_uri, HOLDOUT_ARTIFACT)))
    encoder = _joblib_artifact(artifact_uri, "encoders.pkl").get("store_id")
    stores = [str(v) for v in getattr(encoder, "classes_", [])]
    metrics = {name: values["metrics"] for name, values in training["training_results"].items()}

    bundle = dataset_run.bundle_metadata(
        manifest=manifest,
        conf=conf,
        feature_cols=training["feature_cols"],
        supported_stores=stores,
        ensemble_weights=training["ensemble_weights"],
        split=training["split"],
        metrics=metrics,
    )
    _log_json(training["mlflow_run_id"], bundle, "model_bundle.json")

    report = {
        "training_request_id": conf["training_request_id"],
        "test_window": training["split"]["test"],
        "test_rows": training["test_rows"],
        "metrics": metrics,
        "per_store_wape": {
            model: dataset_run.per_store_wape(holdout, model) for model in MODEL_FAMILIES if model in holdout
        },
        "quality_gate": gate,
        "comparison": comparison,
    }
    _log_json(training["mlflow_run_id"], report, f"{EVALUATION_DIR}/report.json")

    candidates: List[Dict[str, Any]] = []
    from utils.mlflow_utils import MLflowManager

    registry = MLflowManager().registry_name
    for model, version in (registration.get("versions") or {}).items():
        candidates.append(
            {
                "model": model,
                "registered_name": f"{registry}_{model}",
                "version": str(version),
                "gate_passed": bool(gate.get(model, {}).get("passed")),
            }
        )
    return {
        "training_request_id": conf["training_request_id"],
        "mlflow_run_id": training["mlflow_run_id"],
        "candidates": candidates,
        "evaluation": {
            "split": training["split"],
            "test_rows": training["test_rows"],
            "metrics": metrics,
            "quality_gate": gate,
            "comparison": comparison,
            "compatibility": {
                "target": manifest["target"],
                "frequency": manifest["frequency"],
                "currency": manifest["currency"],
                "supported_stores": stores,
                "context_features": manifest.get("context_features") or {},
                "feature_count": len(training["feature_cols"]),
            },
            "ensemble_weights": training["ensemble_weights"],
        },
    }
