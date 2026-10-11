"""
Resolve data provider from ml_config.yaml.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import yaml

from data_providers.base import DataProvider
from data_providers.local_csv import LocalCSVDataProvider
from data_providers.m5 import KaggleM5DataProvider
from data_providers.synthetic import SyntheticRetailDataProvider

DEFAULT_CONFIG_PATH = "/usr/local/airflow/include/config/ml_config.yaml"

PROVIDERS = {
    "synthetic": SyntheticRetailDataProvider,
    "local_csv": LocalCSVDataProvider,
    "kaggle_m5": KaggleM5DataProvider,
}


def load_data_config(config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    if not os.path.isfile(config_path):
        alt = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "ml_config.yaml"
        )
        config_path = alt if os.path.isfile(alt) else config_path
    with open(config_path, encoding="utf-8") as f:
        full = yaml.safe_load(f)
    data = full.get("data") or {}
    return _apply_data_defaults(data)


def _apply_data_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    source = data.get("source", "synthetic")
    version = data.get("dataset_version", "v1.0.0")
    base = "/usr/local/airflow/data/raw"
    defaults = {
        "source": source,
        "dataset_version": version,
        "schema_version": data.get("schema_version", "v1"),
        "random_seed": data.get("random_seed", 42),
        "train_start_date": data.get("train_start_date", "2021-01-01"),
        "train_end_date": data.get("train_end_date", "2021-12-31"),
        "raw_path": data.get("raw_path") or f"{base}/{source}/{version}",
        "processed_path": data.get("processed_path")
        or f"/usr/local/airflow/data/processed/{source}/{version}",
        "regenerate": data.get("regenerate", False),
        "archive_to_minio": data.get("archive_to_minio", False),
        "kaggle_competition": data.get("kaggle_competition", "m5-forecasting-accuracy"),
        "kaggle_download": data.get("kaggle_download", True),
        "kaggle_competition_path": data.get("kaggle_competition_path", ""),
    }
    merged = {**defaults, **data}
    if merged["source"] == "kaggle_m5":
        if merged.get("train_start_date") in (None, "2021-01-01") and "train_start_date" not in data:
            merged["train_start_date"] = "2011-01-29"
        if merged.get("train_end_date") in (None, "2021-12-31") and "train_end_date" not in data:
            merged["train_end_date"] = "2016-04-24"
        if "max_sales_files" not in data:
            merged["max_sales_files"] = 365
    elif "max_sales_files" not in data:
        merged["max_sales_files"] = 50
    return merged


def get_data_provider(config_path: str = DEFAULT_CONFIG_PATH) -> DataProvider:
    data_config = load_data_config(config_path)
    source = data_config["source"]
    cls = PROVIDERS.get(source)
    if cls is None:
        raise ValueError(
            f"Unknown data.source '{source}'. Choose from: {list(PROVIDERS.keys())}"
        )
    return cls(data_config)
