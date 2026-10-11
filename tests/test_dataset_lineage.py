"""Tests for reproducible synthetic data and lineage hashing."""

import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "include"))

from data_providers.synthetic import SyntheticRetailDataProvider
from utils.dataset_lineage import compute_parquet_paths_hash, manifest_matches_config


@pytest.fixture
def synthetic_config():
    return {
        "source": "synthetic",
        "dataset_version": "v1.0.0-test",
        "schema_version": "v1",
        "random_seed": 42,
        "train_start_date": "2021-01-01",
        "train_end_date": "2021-01-03",
        "regenerate": True,
    }


def test_synthetic_same_seed_same_hash(synthetic_config):
    with tempfile.TemporaryDirectory() as tmp:
        cfg1 = {**synthetic_config, "raw_path": os.path.join(tmp, "run1")}
        cfg2 = {**synthetic_config, "raw_path": os.path.join(tmp, "run2")}

        r1 = SyntheticRetailDataProvider(cfg1).provide()
        r2 = SyntheticRetailDataProvider(cfg2).provide()

        assert r1.lineage["dataset_hash"] == r2.lineage["dataset_hash"]
        assert r1.lineage["row_count"] == r2.lineage["row_count"]


def test_manifest_matches_config(synthetic_config):
    manifest = {"config_fingerprint": {
        "source": "synthetic",
        "dataset_version": "v1.0.0-test",
        "schema_version": "v1",
        "random_seed": 42,
        "train_start_date": "2021-01-01",
        "train_end_date": "2021-01-03",
    }}
    assert manifest_matches_config(manifest, synthetic_config)
    assert not manifest_matches_config(manifest, {**synthetic_config, "random_seed": 99})


def test_compute_hash_empty():
    assert compute_parquet_paths_hash([]) == compute_parquet_paths_hash([])
