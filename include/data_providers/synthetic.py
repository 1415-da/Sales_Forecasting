"""
Seeded synthetic retail data — CI, local demos, and reproducible benchmarks.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from data_providers.base import DataProvider, DataProviderResult
from utils.data_generator import RealisticSalesDataGenerator
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


def _collect_existing_sales_paths(output_dir: str) -> List[str]:
    sales_root = os.path.join(output_dir, "sales")
    if not os.path.isdir(sales_root):
        return []
    paths: List[str] = []
    for root, _, files in os.walk(sales_root):
        for name in files:
            if name.endswith(".parquet"):
                paths.append(os.path.join(root, name))
    return sorted(paths)


def _rebuild_file_paths_from_disk(output_dir: str) -> Dict[str, List[str]]:
    file_paths: Dict[str, List[str]] = {
        "sales": _collect_existing_sales_paths(output_dir),
        "inventory": [],
        "customer_traffic": [],
        "promotions": [],
        "store_events": [],
    }
    for key, subdir in [
        ("promotions", "promotions/promotions.parquet"),
        ("store_events", "store_events/events.parquet"),
    ]:
        path = os.path.join(output_dir, subdir)
        if os.path.isfile(path):
            file_paths[key].append(path)
    traffic_root = os.path.join(output_dir, "customer_traffic")
    if os.path.isdir(traffic_root):
        for root, _, files in os.walk(traffic_root):
            for name in files:
                if name.endswith(".parquet"):
                    file_paths["customer_traffic"].append(os.path.join(root, name))
    inv_root = os.path.join(output_dir, "inventory")
    if os.path.isdir(inv_root):
        for root, _, files in os.walk(inv_root):
            for name in files:
                if name.endswith(".parquet"):
                    file_paths["inventory"].append(os.path.join(root, name))
    for key in file_paths:
        file_paths[key] = sorted(file_paths[key])
    return file_paths


class SyntheticRetailDataProvider(DataProvider):
    @property
    def source_name(self) -> str:
        return "synthetic"

    def provide(self) -> DataProviderResult:
        cfg = self.data_config
        output_dir = cfg["raw_path"]
        start = cfg["train_start_date"]
        end = cfg["train_end_date"]
        seed = int(cfg.get("random_seed", 42))
        version = cfg.get("dataset_version", "v1.0.0")
        schema_version = cfg.get("schema_version", "v1")
        regenerate = bool(cfg.get("regenerate", False))

        existing_manifest = load_manifest(output_dir)
        if (
            not regenerate
            and existing_manifest
            and manifest_matches_config(existing_manifest, cfg)
            and _collect_existing_sales_paths(output_dir)
        ):
            logger.info("Reusing cached synthetic dataset at %s", output_dir)
            file_paths = _rebuild_file_paths_from_disk(output_dir)
            lineage = existing_manifest.get("lineage", {})
            lineage["regenerated"] = False
            total = sum(len(v) for v in file_paths.values())
            return DataProviderResult(
                data_output_dir=output_dir,
                file_paths=file_paths,
                total_files=total,
                lineage=lineage,
            )

        os.makedirs(output_dir, exist_ok=True)
        generator = RealisticSalesDataGenerator(
            start_date=start,
            end_date=end,
            random_seed=seed,
            dataset_version=version,
        )
        logger.info(
            "Generating synthetic dataset version=%s seed=%s range=%s..%s",
            version,
            seed,
            start,
            end,
        )
        file_paths = generator.generate_sales_data(output_dir=output_dir)

        sales_files = file_paths.get("sales", [])
        dataset_hash = compute_parquet_paths_hash(sales_files)
        row_count = count_sales_rows(sales_files)
        distinct = count_distinct_from_sales(sales_files, ["store_id", "product_id"])
        file_counts = {k: len(v) for k, v in file_paths.items()}

        lineage: Dict[str, Any] = {
            "data_source": self.source_name,
            "dataset_version": version,
            "schema_version": schema_version,
            "random_seed": seed,
            "date_range_start": start,
            "date_range_end": end,
            "dataset_hash": dataset_hash,
            "row_count": row_count,
            "store_count": distinct.get("store_id", 0),
            "product_count": distinct.get("product_id", 0),
            "raw_path": output_dir,
            "file_counts": file_counts,
            "regenerated": True,
        }

        save_manifest(
            output_dir,
            {
                "config_fingerprint": config_fingerprint(cfg),
                "lineage": lineage,
            },
        )

        total = sum(len(v) for v in file_paths.values())
        return DataProviderResult(
            data_output_dir=output_dir,
            file_paths=file_paths,
            total_files=total,
            lineage=lineage,
        )
