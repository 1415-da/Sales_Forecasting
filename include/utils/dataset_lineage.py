"""
Dataset lineage helpers: manifests, content hashing, and MLflow param formatting.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


MANIFEST_FILENAME = "dataset_manifest.json"


@dataclass
class DatasetLineage:
    """Metadata describing a versioned training dataset."""

    data_source: str
    dataset_version: str
    schema_version: str
    random_seed: Optional[int]
    date_range_start: str
    date_range_end: str
    dataset_hash: str
    row_count: int
    store_count: int
    product_count: int
    raw_path: str
    file_counts: Dict[str, int] = field(default_factory=dict)
    regenerated: bool = True

    def to_mlflow_params(self) -> Dict[str, Any]:
        """Flat params for MLflow (string values only where needed)."""
        params = {
            "data.source": self.data_source,
            "data.version": self.dataset_version,
            "data.schema_version": self.schema_version,
            "data.hash": self.dataset_hash,
            "data.rows": self.row_count,
            "data.stores": self.store_count,
            "data.products": self.product_count,
            "data.date_min": self.date_range_start,
            "data.date_max": self.date_range_end,
            "data.raw_path": self.raw_path,
            "data.regenerated": str(self.regenerated),
        }
        if self.random_seed is not None:
            params["data.random_seed"] = self.random_seed
        for key, count in self.file_counts.items():
            params[f"data.files.{key}"] = count
        return params

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def config_fingerprint(data_config: Dict[str, Any]) -> Dict[str, Any]:
    """Stable subset of config used to decide whether cached data is still valid."""
    fp = {
        "source": data_config.get("source"),
        "dataset_version": data_config.get("dataset_version"),
        "schema_version": data_config.get("schema_version"),
        "random_seed": data_config.get("random_seed"),
        "train_start_date": data_config.get("train_start_date"),
        "train_end_date": data_config.get("train_end_date"),
    }
    if data_config.get("source") == "kaggle_m5":
        fp["kaggle_competition"] = data_config.get("kaggle_competition")
        fp["kaggle_download"] = data_config.get("kaggle_download")
    return fp


def save_manifest(raw_path: str, manifest: Dict[str, Any]) -> str:
    os.makedirs(raw_path, exist_ok=True)
    path = os.path.join(raw_path, MANIFEST_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return path


def load_manifest(raw_path: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(raw_path, MANIFEST_FILENAME)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def manifest_matches_config(manifest: Dict[str, Any], data_config: Dict[str, Any]) -> bool:
    expected = config_fingerprint(data_config)
    stored = manifest.get("config_fingerprint", {})
    return stored == expected


def compute_parquet_paths_hash(file_paths: List[str], chunk_size: int = 1024 * 1024) -> str:
    """SHA-256 over sorted parquet file contents (streaming).

    Uses basename only (not absolute path) so the same dataset materialized
    under different directories yields the same hash.
    """
    digest = hashlib.sha256()
    for path in sorted(file_paths, key=os.path.basename):
        if not os.path.isfile(path):
            continue
        digest.update(os.path.basename(path).encode("utf-8"))
        with open(path, "rb") as f:
            while True:
                block = f.read(chunk_size)
                if not block:
                    break
                digest.update(block)
    return digest.hexdigest()


def count_sales_rows(sales_files: List[str], max_files: Optional[int] = None) -> int:
    import pandas as pd

    total = 0
    paths = sales_files if max_files is None else sales_files[:max_files]
    for path in paths:
        if os.path.isfile(path):
            total += len(pd.read_parquet(path))
    return total


def count_distinct_from_sales(
    sales_files: List[str], columns: List[str], sample_limit: int = 50
) -> Dict[str, int]:
    import pandas as pd

    frames = []
    for path in sales_files[:sample_limit]:
        if os.path.isfile(path):
            frames.append(pd.read_parquet(path, columns=columns))
    if not frames:
        return {col: 0 for col in columns}
    combined = pd.concat(frames, ignore_index=True)
    return {col: int(combined[col].nunique()) for col in columns if col in combined.columns}
