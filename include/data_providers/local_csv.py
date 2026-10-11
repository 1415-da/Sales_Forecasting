"""
Local CSV / Parquet retail data under a versioned raw_path.

Expected layout (after one-time preparation):

data/raw/retail/v1.0.0/
    dataset_manifest.json   # optional; created on first ingest
    sales/                  # partitioned parquet (same as synthetic), OR
    sales.csv               # will be converted to sales/**/*.parquet
    promotions/promotions.parquet   (optional)
    customer_traffic/...            (optional)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import pandas as pd

from data_providers.base import DataProvider, DataProviderResult
from data_providers.synthetic import _rebuild_file_paths_from_disk
from utils.dataset_lineage import (
    compute_parquet_paths_hash,
    config_fingerprint,
    count_distinct_from_sales,
    count_sales_rows,
    load_manifest,
    manifest_matches_config,
    save_manifest,
)

logger = logging.getLogger(__name__)


def _csv_to_partitioned_parquet(csv_path: str, output_dir: str) -> List[str]:
    df = pd.read_csv(csv_path)
    if "date" not in df.columns:
        raise ValueError(f"{csv_path} must contain a 'date' column")
    df["date"] = pd.to_datetime(df["date"])
    paths: List[str] = []
    for date, group in df.groupby(df["date"].dt.date):
        ts = pd.Timestamp(date)
        date_str = ts.strftime("%Y-%m-%d")
        sales_path = os.path.join(
            output_dir,
            f"sales/year={ts.year}/month={ts.month:02d}/day={ts.day:02d}/"
            f"sales_{date_str}.parquet",
        )
        os.makedirs(os.path.dirname(sales_path), exist_ok=True)
        group.to_parquet(sales_path, index=False)
        paths.append(sales_path)
    return sorted(paths)


class LocalCSVDataProvider(DataProvider):
    @property
    def source_name(self) -> str:
        return "local_csv"

    def provide(self) -> DataProviderResult:
        cfg = self.data_config
        output_dir = cfg["raw_path"]
        version = cfg.get("dataset_version", "v1.0.0")
        schema_version = cfg.get("schema_version", "v1")
        regenerate = bool(cfg.get("regenerate", False))

        existing_manifest = load_manifest(output_dir)
        sales_on_disk = _rebuild_file_paths_from_disk(output_dir).get("sales", [])
        if (
            not regenerate
            and existing_manifest
            and manifest_matches_config(existing_manifest, cfg)
            and sales_on_disk
        ):
            file_paths = _rebuild_file_paths_from_disk(output_dir)
            lineage = existing_manifest.get("lineage", {})
            lineage["regenerated"] = False
            return DataProviderResult(
                data_output_dir=output_dir,
                file_paths=file_paths,
                total_files=sum(len(v) for v in file_paths.values()),
                lineage=lineage,
            )

        os.makedirs(output_dir, exist_ok=True)
        csv_path = os.path.join(output_dir, "sales.csv")
        if not sales_on_disk and os.path.isfile(csv_path):
            logger.info("Ingesting %s into partitioned parquet", csv_path)
            _csv_to_partitioned_parquet(csv_path, output_dir)

        file_paths = _rebuild_file_paths_from_disk(output_dir)
        sales_files = file_paths.get("sales", [])
        if not sales_files:
            raise FileNotFoundError(
                f"No sales data under {output_dir}. Add sales/**/*.parquet or sales.csv"
            )

        dataset_hash = compute_parquet_paths_hash(sales_files)
        row_count = count_sales_rows(sales_files)
        distinct = count_distinct_from_sales(
            sales_files, ["store_id", "product_id"], sample_limit=len(sales_files)
        )
        dates = []
        for path in sales_files[: min(5, len(sales_files))]:
            dates.append(pd.read_parquet(path, columns=["date"])["date"])
        date_series = pd.concat(dates, ignore_index=True)
        date_min = str(pd.to_datetime(date_series.min()).date())
        date_max = str(pd.to_datetime(date_series.max()).date())

        lineage: Dict[str, Any] = {
            "data_source": self.source_name,
            "dataset_version": version,
            "schema_version": schema_version,
            "random_seed": None,
            "date_range_start": cfg.get("train_start_date") or date_min,
            "date_range_end": cfg.get("train_end_date") or date_max,
            "dataset_hash": dataset_hash,
            "row_count": row_count,
            "store_count": distinct.get("store_id", 0),
            "product_count": distinct.get("product_id", 0),
            "raw_path": output_dir,
            "file_counts": {k: len(v) for k, v in file_paths.items()},
            "regenerated": True,
        }

        save_manifest(
            output_dir,
            {"config_fingerprint": config_fingerprint(cfg), "lineage": lineage},
        )

        return DataProviderResult(
            data_output_dir=output_dir,
            file_paths=file_paths,
            total_files=sum(len(v) for v in file_paths.values()),
            lineage=lineage,
        )
