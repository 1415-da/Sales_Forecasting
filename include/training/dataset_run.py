"""
The dataset-driven side of the training DAG, as plain functions.

The DAG tasks are thin wrappers around these so each step -- load and verify
the exact dataset, re-validate it, gate the candidate, compare it with the
current model, describe the bundle -- can be tested without Airflow, MLflow,
or MinIO running.
"""

from __future__ import annotations

import json
import math
import platform
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from training import contract
from training.configs import DEPLOYMENT_POLICY, MODEL_FAMILIES
from training.storage import DatasetStore, parse_uri

#: Same-day inputs that restate the target; a feature schema containing one
#: is refused because serving would have to guess it.
LEAKAGE_COLUMNS = ("quantity_sold", "profit")

#: How closely reloaded predictions must reproduce training's, in sales units.
#: The models predict in float32 and the trainer combines ensemble members in
#: different arithmetic, so near-zero days (closures) differ by ~1e-3; a
#: hundredth of a unit is far below any meaningful difference.
REPRODUCTION_ATOL = 0.01

CONF_KEYS = (
    "training_request_id",
    "dataset_id",
    "dataset_version",
    "manifest_uri",
    "manifest_sha256",
    "content_sha256",
    "training_config_id",
    "deployment_policy",
)


class DatasetIntegrityError(RuntimeError):
    """The stored dataset does not match what the request was approved for."""


def is_dataset_run(conf: Optional[Dict[str, Any]]) -> bool:
    """Whether a dag run was started for a training request."""
    return bool(conf and conf.get("training_request_id"))


def require_conf(conf: Dict[str, Any]) -> Dict[str, Any]:
    """The request's metadata, refusing a run that is missing any of it."""
    missing = [key for key in CONF_KEYS if not conf.get(key)]
    if missing:
        raise ValueError(f"Training request conf is missing: {', '.join(missing)}")
    if conf["deployment_policy"] != DEPLOYMENT_POLICY:
        raise ValueError(f"Unsupported deployment policy {conf['deployment_policy']!r}")
    return conf


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_dataset(
    conf: Dict[str, Any], store: Optional[DatasetStore] = None
) -> Tuple[Dict[str, Any], pd.DataFrame]:
    """
    Load the exact dataset version the request names, and prove it.

    Three hashes must agree: the manifest's bytes with the hash the API
    recorded, the Parquet object with the manifest, and the decoded records
    with the content hash. Any mismatch stops the run -- training on data
    other than what was validated is the failure this guards against.
    """
    store = store or DatasetStore()
    _, manifest_key = parse_uri(conf["manifest_uri"])
    manifest_bytes = store.get(manifest_key)
    if contract.sha256_bytes(manifest_bytes) != conf["manifest_sha256"]:
        raise DatasetIntegrityError("The dataset manifest does not match the approved version.")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("dataset_id") != conf["dataset_id"]:
        raise DatasetIntegrityError("The manifest belongs to a different dataset.")

    processed = manifest["processed"]
    expected_prefix = f"datasets/{conf['dataset_id']}/processed/"
    if not str(processed["object_key"]).startswith(expected_prefix):
        raise DatasetIntegrityError("The manifest points outside its dataset.")
    content = store.get(processed["object_key"])
    if contract.sha256_bytes(content) != processed["object_sha256"]:
        raise DatasetIntegrityError("The processed dataset object was modified.")
    frame = contract.from_parquet_bytes(content)
    digest = contract.content_hash(frame)
    if digest != processed["content_sha256"] or digest != conf["content_sha256"]:
        raise DatasetIntegrityError("The dataset records do not match their content hash.")
    return manifest, frame


def policy_from_manifest(manifest: Dict[str, Any]) -> contract.ValidationPolicy:
    """The validation policy the dataset was sealed under."""
    stored = (manifest.get("validation") or {}).get("policy") or {}
    allowed = stored.get("allowed_stores")
    return contract.ValidationPolicy(
        max_bytes=int(stored.get("max_bytes", contract.ValidationPolicy.max_bytes)),
        max_rows=int(stored.get("max_rows", contract.ValidationPolicy.max_rows)),
        negative_sales=stored.get("negative_sales", "reject"),
        allowed_stores=tuple(allowed) if allowed else None,
        allowed_currencies=tuple(stored.get("allowed_currencies") or ("USD",)),
        warmup_days=int(stored.get("feature_warmup_days", 30)),
        min_train_days_after_warmup=int(stored.get("min_store_days", 60)) - int(stored.get("feature_warmup_days", 30)),
        validation_fraction=float(stored.get("validation_fraction", 0.1)),
        test_fraction=float(stored.get("test_fraction", 0.2)),
        min_validation_days=int(stored.get("min_validation_days", 14)),
        min_test_days=int(stored.get("min_test_days", 28)),
    )


def revalidate(manifest: Dict[str, Any], frame: pd.DataFrame) -> Dict[str, Any]:
    """Run the full contract over every record again, as sealed."""
    result = contract.validate(
        contract.frame_to_raw(frame),
        policy_from_manifest(manifest),
        currency=manifest["currency"],
        source_type=(manifest.get("source_types") or ["real"])[0],
        options=feature_options(manifest),
    )
    if not result.is_valid:
        codes = ", ".join(contract.iter_issue_codes(result.report))
        raise DatasetIntegrityError(f"The dataset no longer passes validation: {codes}")
    report = result.report
    return {
        "rows": report["rows_valid"],
        "stores": report["store_count"],
        "dates": report["total_dates"],
        "date_min": report["date_min"],
        "date_max": report["date_max"],
        "split": report["split"],
        "warnings": [issue["code"] for issue in report["warnings"]],
    }


def feature_options(manifest: Dict[str, Any]) -> contract.FeatureOptions:
    """The feature policies the dataset was sealed with (defaults for v1 manifests)."""
    return contract.FeatureOptions.from_dict(manifest.get("feature_options"))


def configure_feature_engineer(engineer: Any, manifest: Dict[str, Any]) -> None:
    """Drop the US holiday feature for sources it does not describe."""
    if feature_options(manifest).holiday_calendar == "none":
        date_features = list(engineer.feature_config.get("date_features") or [])
        engineer.feature_config["date_features"] = [name for name in date_features if name != "is_holiday"]


def training_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """The store-day frame the trainer expects (same columns as the M5 path)."""
    out = contract.canonical_frame(frame).copy()
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values(["date", "store_id"]).reset_index(drop=True)


def lineage_params(manifest: Dict[str, Any], conf: Dict[str, Any]) -> Dict[str, Any]:
    """Dataset lineage in the shape ``ModelTrainer`` logs to MLflow."""
    return {
        "data_source": "uploaded",
        "dataset_version": f"{manifest['name']} v{manifest['version']}",
        "schema_version": manifest["schema_version"],
        "random_seed": None,
        "date_range_start": manifest["date_min"],
        "date_range_end": manifest["date_max"],
        "dataset_hash": manifest["processed"]["content_sha256"],
        "row_count": manifest["row_count"],
        "store_count": manifest["store_count"],
        "product_count": 0,
        "raw_path": conf["manifest_uri"],
        "file_counts": {"sources": len(manifest.get("sources") or [])},
        "regenerated": False,
    }


def run_tags(manifest: Dict[str, Any], conf: Dict[str, Any]) -> Dict[str, str]:
    return {
        "training_request_id": conf["training_request_id"],
        "dataset_id": conf["dataset_id"],
        "dataset_adapter": str((manifest.get("adapter") or {}).get("id") or "canonical_store_day"),
        "source_namespace": str(manifest.get("source_namespace") or "canonical"),
        "dataset_name": str(manifest["name"]),
        "dataset_version": str(manifest["version"]),
        "dataset_content_sha256": manifest["processed"]["content_sha256"],
        "dataset_manifest_sha256": conf["manifest_sha256"],
        "training_config_id": conf["training_config_id"],
        "deployment_policy": conf["deployment_policy"],
        "source_types": ",".join(manifest.get("source_types") or []),
    }


# ---------------------------------------------------------------------------
# Evaluation and gating
# ---------------------------------------------------------------------------


def wape(actual: Sequence[float], predicted: Sequence[float]) -> Optional[float]:
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    total = float(np.abs(actual).sum())
    if total <= 0 or len(actual) == 0:
        return None
    return float(np.abs(actual - predicted).sum() / total * 100)


def holdout_table(test_df: pd.DataFrame, predictions: Dict[str, Sequence[float]]) -> pd.DataFrame:
    """Per-row test predictions, for the evaluation artifact."""
    table = pd.DataFrame(
        {
            "date": pd.to_datetime(test_df["date"]).dt.strftime("%Y-%m-%d").values,
            "store_id": test_df["store_id"].astype(str).values,
            "actual": test_df["sales"].astype(float).values,
        }
    )
    for name, values in predictions.items():
        table[name] = np.asarray(values, dtype=float)
    return table


def per_store_wape(table: pd.DataFrame, model: str) -> Dict[str, Optional[float]]:
    return {
        store: wape(group["actual"], group[model])
        for store, group in table.groupby("store_id", sort=True)
    }


def quality_gate(
    *,
    model: str,
    loaded_feature_cols: Optional[Sequence[str]],
    trained_feature_cols: Sequence[str],
    predict: Optional[Callable[[], np.ndarray]],
    in_memory_predictions: Sequence[float],
    supported_stores: Sequence[str],
    dataset_stores: Sequence[str],
    test_rows: int,
    min_test_rows: int,
    metrics: Dict[str, float],
    load_error: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Every check a candidate must pass to be offered for deployment.

    ``predict`` re-runs the *reloaded* artifacts on the test matrix: a model
    that only works from the training process's memory is not a deployable
    bundle.
    """
    checks: List[Dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    check(
        "artifacts_load",
        load_error is None and loaded_feature_cols is not None and predict is not None,
        load_error or "Model, scaler, encoders and feature list reloaded from the run.",
    )
    cols = list(loaded_feature_cols or [])
    leaked = [column for column in cols if column in LEAKAGE_COLUMNS]
    contract_ok = (
        bool(cols)
        and cols == list(trained_feature_cols)
        and len(set(cols)) == len(cols)
        and not leaked
    )
    check(
        "feature_contract",
        contract_ok,
        f"{len(cols)} ordered features match training."
        if contract_ok
        else (
            f"Same-day target columns present: {leaked}" if leaked else "Reloaded feature list differs from training."
        ),
    )
    finite = False
    detail = "Prediction was not attempted."
    if predict is not None and load_error is None:
        try:
            reloaded = np.asarray(predict(), dtype=float)
            expected = np.asarray(in_memory_predictions, dtype=float)
            finite = (
                reloaded.shape == (test_rows,)
                and bool(np.isfinite(reloaded).all())
                and np.allclose(reloaded, expected, rtol=1e-5, atol=REPRODUCTION_ATOL)
            )
            detail = (
                f"{test_rows} finite predictions, matching the training process within {REPRODUCTION_ATOL} sales units."
                if finite
                else f"Shape {reloaded.shape} / finite={bool(np.isfinite(reloaded).all())} / "
                "differs from in-memory predictions."
            )
        except Exception as exc:  # noqa: BLE001 - a failed predict is a failed gate
            detail = f"Reloaded model failed to predict: {type(exc).__name__}"
    check("predictions_finite", finite, detail)
    stores_ok = sorted(map(str, supported_stores)) == sorted(map(str, dataset_stores))
    check(
        "store_support",
        stores_ok,
        f"Encoder covers the dataset's {len(dataset_stores)} store(s)."
        if stores_ok
        else "Encoder stores differ from the dataset's stores.",
    )
    check(
        "evaluation_sample",
        test_rows >= min_test_rows,
        f"{test_rows} scored test rows (policy minimum {min_test_rows}).",
    )
    metric_ok = all(
        isinstance(metrics.get(name), (int, float)) and math.isfinite(metrics[name])
        for name in ("rmse", "mae", "wape")
    )
    check("metrics_present", metric_ok, "RMSE, MAE and WAPE recorded." if metric_ok else "A required metric is missing.")
    return {"model": model, "passed": all(item["passed"] for item in checks), "checks": checks}


def champion_comparison(
    *,
    champion: Optional[Dict[str, Any]],
    test_window: Sequence[str],
    test_features: pd.DataFrame,
    actual: Sequence[float],
    candidate_predictions: Sequence[float],
    champion_predict: Optional[Callable[[pd.DataFrame], np.ndarray]],
) -> Dict[str, Any]:
    """
    Score the current Production model on the candidate's test rows -- or say
    why that would not be a fair comparison.

    Fair means: the same rows, the same features, and a champion that never
    trained on those dates. A champion whose training data reaches into the
    test window has seen the answers, and its score would flatter it.
    """
    if not champion:
        return {"comparable": False, "reason": "There is no current Production model to compare with."}
    base = {
        "champion_model": champion.get("model"),
        "champion_version": champion.get("version"),
        "test_window": list(test_window),
    }
    trained_until = champion.get("data_date_max")
    if trained_until and str(trained_until) >= str(test_window[0]):
        return {
            **base,
            "comparable": False,
            "reason": (
                f"Not directly comparable: version {champion.get('version')} was trained on data "
                f"up to {trained_until}, which overlaps this test window ({test_window[0]} to "
                f"{test_window[1]}), so its score on these days would be optimistic."
            ),
        }
    missing = [column for column in champion.get("feature_cols") or [] if column not in test_features.columns]
    if missing:
        return {
            **base,
            "comparable": False,
            "reason": f"Not directly comparable: this dataset cannot supply the current model's inputs {missing[:5]}.",
        }
    known = set(champion.get("stores") or [])
    mask = test_features["store_id"].astype(str).isin(known).to_numpy()
    if not mask.any():
        return {
            **base,
            "comparable": False,
            "reason": "Not directly comparable: the current model serves none of this dataset's stores.",
        }
    if champion_predict is None:
        return {**base, "comparable": False, "reason": "The current model's artifacts could not be loaded."}
    try:
        champion_values = np.asarray(champion_predict(test_features[mask]), dtype=float)
    except Exception as exc:  # noqa: BLE001
        return {**base, "comparable": False, "reason": f"The current model failed on these rows: {type(exc).__name__}"}
    actual = np.asarray(actual, dtype=float)[mask]
    candidate = np.asarray(candidate_predictions, dtype=float)[mask]
    champion_wape = wape(actual, champion_values)
    candidate_wape = wape(actual, candidate)
    return {
        **base,
        "comparable": True,
        "rows": int(mask.sum()),
        "rows_excluded": int((~mask).sum()),
        "candidate_wape": candidate_wape,
        "champion_wape": champion_wape,
        "wape_change_points": (
            None if champion_wape is None or candidate_wape is None else candidate_wape - champion_wape
        ),
        "reason": (
            f"Both models scored on the same {int(mask.sum())} test rows; "
            f"{int((~mask).sum())} rows for stores the current model does not serve were excluded."
        ),
    }


def bundle_metadata(
    *,
    manifest: Dict[str, Any],
    conf: Dict[str, Any],
    feature_cols: Sequence[str],
    supported_stores: Sequence[str],
    ensemble_weights: Dict[str, float],
    split: Dict[str, Any],
    metrics: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    """The serving contract of the candidate bundle, logged as ``model_bundle.json``."""
    def version_of(module: str) -> Optional[str]:
        try:
            return __import__(module).__version__
        except Exception:  # noqa: BLE001
            return None

    return {
        "bundle_schema": "model_bundle_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "training_request_id": conf["training_request_id"],
        "target": manifest["target"],
        "frequency": manifest["frequency"],
        "currency": manifest["currency"],
        "grain": "store-day",
        "feature_schema": list(feature_cols),
        "context_features": manifest.get("context_features") or {},
        "supported_stores": sorted(map(str, supported_stores)),
        "models": list(MODEL_FAMILIES),
        "ensemble": {"components": sorted(ensemble_weights), "weights": ensemble_weights},
        "preprocessing": {
            "scaler": "StandardScaler (scalers.pkl, key 'standard'), fitted on the training window only",
            "encoders": "LabelEncoder per categorical column (encoders.pkl), fitted on the training window only",
        },
        "uncertainty": {
            "method": "holdout_residual",
            "detail": "Serving draws interval width from <model>_rmse measured on the untouched test window.",
            "rmse": {name: values.get("rmse") for name, values in metrics.items()},
        },
        "evaluation": {"split": split, "metrics": metrics},
        "dataset": {
            "id": conf["dataset_id"],
            "name": manifest["name"],
            "version": manifest["version"],
            "manifest_uri": conf["manifest_uri"],
            "manifest_sha256": conf["manifest_sha256"],
            "content_sha256": manifest["processed"]["content_sha256"],
            "source_types": manifest.get("source_types"),
        },
        "environment": {
            "python": platform.python_version(),
            "pandas": version_of("pandas"),
            "numpy": version_of("numpy"),
            "scikit-learn": version_of("sklearn"),
            "xgboost": version_of("xgboost"),
            "lightgbm": version_of("lightgbm"),
        },
    }


def artifact_key(artifact_uri: str, relative: str) -> Tuple[str, str]:
    """``(bucket, key)`` of a run artifact under an ``s3://`` artifact root."""
    if not artifact_uri.startswith("s3://"):
        raise ValueError(f"Unsupported artifact store: {artifact_uri}")
    bucket, _, prefix = artifact_uri[len("s3://"):].partition("/")
    return bucket, f"{prefix.rstrip('/')}/{relative}"
