"""
Kaggle M5 Forecasting adapter (store-day aggregation).

Downloads competition files via kagglehub when ``kaggle_download: true``,
then ingests CSVs into versioned parquet under ``raw_path``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

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

M5_SALES_CANDIDATES = [
    "sales_train_validation.csv",
    "sales_train_evaluation.csv",
]


def _find_file(base_dir: str, filenames: List[str]) -> str:
    """Locate a file in base_dir or any subdirectory (kagglehub layout)."""
    for name in filenames:
        direct = os.path.join(base_dir, name)
        if os.path.isfile(direct):
            return direct
    for root, _, files in os.walk(base_dir):
        for name in filenames:
            if name in files:
                return os.path.join(root, name)
    raise FileNotFoundError(
        f"None of {filenames} found under {base_dir}. "
        "Ensure Kaggle credentials are configured for kagglehub."
    )


def download_kaggle_competition(competition: str) -> str:
    """Download competition data using kagglehub; returns local directory path."""
    import kagglehub

    logger.info("Downloading Kaggle competition '%s' via kagglehub...", competition)
    path = kagglehub.competition_download(competition)
    logger.info("Kaggle competition files at: %s", path)
    return path


def resolve_kaggle_source_dir(data_config: Dict[str, Any]) -> str:
    """
    Resolve directory containing M5 CSVs: explicit path, raw_path fallback, or kagglehub download.
    """
    explicit = (data_config.get("kaggle_competition_path") or "").strip()
    if explicit and os.path.isdir(explicit):
        logger.info("Using configured kaggle_competition_path: %s", explicit)
        return explicit

    raw_path = data_config.get("raw_path", "")
    if not data_config.get("kaggle_download", True):
        if raw_path and os.path.isdir(raw_path):
            try:
                _find_file(raw_path, ["calendar.csv"])
                return raw_path
            except FileNotFoundError:
                pass
        raise FileNotFoundError(
            "kaggle_download is false but no M5 CSVs found. "
            "Set kaggle_competition_path or place files under raw_path."
        )

    competition = data_config.get("kaggle_competition", "m5-forecasting-accuracy")
    return download_kaggle_competition(competition)


def _ingest_m5_to_store_day_parquet(
    kaggle_source_dir: str,
    output_dir: str,
    train_start: Optional[str] = None,
    train_end: Optional[str] = None,
    sales_file_names: Optional[List[str]] = None,
) -> str:
    """Transform M5 wide sales + calendar into daily partitioned parquet."""
    names = sales_file_names or M5_SALES_CANDIDATES
    sales_path = _find_file(kaggle_source_dir, names)
    calendar_path = _find_file(kaggle_source_dir, ["calendar.csv"])

    logger.info("Loading M5 sales (this may take several minutes): %s", sales_path)
    sales = pd.read_csv(sales_path)
    calendar = pd.read_csv(calendar_path)

    id_cols = [c for c in sales.columns if c.startswith("d_")]
    meta_cols = [
        c
        for c in ["id", "item_id", "dept_id", "cat_id", "store_id", "state_id"]
        if c in sales.columns
    ]
    long_df = sales.melt(
        id_vars=meta_cols, value_vars=id_cols, var_name="d", value_name="units_sold"
    )
    long_df = long_df.merge(calendar[["d", "date"]], on="d", how="left")
    long_df["date"] = pd.to_datetime(long_df["date"])
    long_df = long_df.rename(columns={"item_id": "product_id"})

    agg = (
        long_df.groupby(["date", "store_id", "product_id"], as_index=False)
        .agg({"units_sold": "sum", "cat_id": "first"})
        .rename(columns={"units_sold": "quantity_sold", "cat_id": "category"})
    )

    if train_start:
        agg = agg[agg["date"] >= pd.to_datetime(train_start)]
    if train_end:
        agg = agg[agg["date"] <= pd.to_datetime(train_end)]

    agg["revenue"] = agg["quantity_sold"].astype(float)
    agg["unit_price"] = 1.0
    agg["discount_percent"] = 0.0
    agg["cost"] = agg["revenue"] * 0.7
    agg["profit"] = agg["revenue"] - agg["cost"]

    if "event_name_1" in calendar.columns:
        events = calendar[["date", "event_name_1"]].copy()
        events["date"] = pd.to_datetime(events["date"])
        events["has_event"] = events["event_name_1"].notna().astype(int)
        promo_out = os.path.join(output_dir, "promotions")
        os.makedirs(promo_out, exist_ok=True)
        events.to_parquet(os.path.join(promo_out, "promotions.parquet"), index=False)

    for date, group in agg.groupby(agg["date"].dt.date):
        ts = pd.Timestamp(date)
        date_str = ts.strftime("%Y-%m-%d")
        out_path = os.path.join(
            output_dir,
            f"sales/year={ts.year}/month={ts.month:02d}/day={ts.day:02d}/"
            f"sales_{date_str}.parquet",
        )
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        group.to_parquet(out_path, index=False)

    return sales_path


class KaggleM5DataProvider(DataProvider):
    @property
    def source_name(self) -> str:
        return "kaggle_m5"

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
        kaggle_dir = resolve_kaggle_source_dir(cfg)
        sales_csv_used = _ingest_m5_to_store_day_parquet(
            kaggle_dir,
            output_dir,
            train_start=cfg.get("train_start_date"),
            train_end=cfg.get("train_end_date"),
        )

        file_paths = _rebuild_file_paths_from_disk(output_dir)
        sales_files = file_paths.get("sales", [])
        if not sales_files:
            raise FileNotFoundError("M5 ingest produced no sales parquet files")

        dataset_hash = compute_parquet_paths_hash(sales_files)
        row_count = count_sales_rows(sales_files)
        distinct = count_distinct_from_sales(
            sales_files,
            ["store_id", "product_id"],
            sample_limit=min(100, len(sales_files)),
        )

        start = cfg.get("train_start_date") or "2011-01-29"
        end = cfg.get("train_end_date") or "2016-06-19"

        lineage: Dict[str, Any] = {
            "data_source": self.source_name,
            "dataset_version": version,
            "schema_version": schema_version,
            "random_seed": None,
            "date_range_start": start,
            "date_range_end": end,
            "dataset_hash": dataset_hash,
            "row_count": row_count,
            "store_count": distinct.get("store_id", 0),
            "product_count": distinct.get("product_id", 0),
            "raw_path": output_dir,
            "file_counts": {k: len(v) for k, v in file_paths.items()},
            "regenerated": True,
            "kaggle_competition": cfg.get("kaggle_competition", "m5-forecasting-accuracy"),
            "kaggle_source_dir": kaggle_dir,
            "kaggle_sales_csv": sales_csv_used,
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
