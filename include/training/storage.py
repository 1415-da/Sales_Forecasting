"""
Write-once object storage for training datasets.

Both the API (which writes) and the Airflow workers (which read) reach MinIO by
its Docker service name, so the dataset the DAG loads is the bytes the API
validated, not a copy on one container's disk.

Keys are generated here from the dataset id and content hash. Nothing a user
typed -- not the file name, not the dataset name -- becomes part of a key.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

DEFAULT_BUCKET = "training-datasets"
_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ImmutableObjectError(RuntimeError):
    """A key already holds different bytes; datasets are never overwritten."""


class StorageUnavailableError(RuntimeError):
    """The object store could not be reached."""


def bucket_name() -> str:
    return os.getenv("TRAINING_DATASET_BUCKET", DEFAULT_BUCKET)


def _check_id(dataset_id: str) -> str:
    if not _ID_PATTERN.match(dataset_id or ""):
        raise ValueError("dataset id must be 32 lowercase hex characters")
    return dataset_id


def _check_sha(sha: str) -> str:
    if not _SHA_PATTERN.match(sha or ""):
        raise ValueError("hash must be a sha256 hex digest")
    return sha


def raw_key(dataset_id: str, sha256: str, file_format: str) -> str:
    extension = {"csv": "csv", "parquet": "parquet"}[file_format]
    return f"datasets/{_check_id(dataset_id)}/source/{_check_sha(sha256)}.{extension}"


def processed_key(dataset_id: str, content_sha256: str) -> str:
    return f"datasets/{_check_id(dataset_id)}/processed/{_check_sha(content_sha256)}.parquet"


def report_key(dataset_id: str) -> str:
    return f"datasets/{_check_id(dataset_id)}/validation_report.json"


def manifest_key(dataset_id: str) -> str:
    return f"datasets/{_check_id(dataset_id)}/manifest.json"


def to_uri(key: str) -> str:
    return f"s3://{bucket_name()}/{key}"


def parse_uri(uri: str) -> Tuple[str, str]:
    """``(bucket, key)`` of a dataset URI, refusing anything outside the bucket."""
    prefix = f"s3://{bucket_name()}/"
    if not uri or not uri.startswith(prefix):
        raise ValueError(f"not a training dataset URI: {uri!r}")
    key = uri[len(prefix):]
    if not key.startswith("datasets/") or ".." in key.split("/"):
        raise ValueError(f"not a training dataset key: {key!r}")
    return bucket_name(), key


class DatasetStore:
    """S3-compatible client wrapper with write-once semantics."""

    def __init__(self, client: Any = None, bucket: Optional[str] = None) -> None:
        self._client = client
        self.bucket = bucket or bucket_name()
        self._bucket_checked = False

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=os.getenv("TRAINING_S3_ENDPOINT_URL")
                or os.getenv("MLFLOW_S3_ENDPOINT_URL", "http://minio:9000"),
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                config=Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 3}),
            )
        return self._client

    def ensure_bucket(self) -> None:
        if self._bucket_checked:
            return
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:  # noqa: BLE001 - create on any "missing" answer
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchBucket", "NotFound"):
                self.client.create_bucket(Bucket=self.bucket)
            else:
                raise StorageUnavailableError(f"Object storage is unavailable: {exc}") from exc
        self._bucket_checked = True

    def _existing_sha(self, key: str) -> Optional[str]:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageUnavailableError(f"Object storage is unavailable: {exc}") from exc
        return (head.get("Metadata") or {}).get("sha256", "")

    def put_once(self, key: str, content: bytes, sha256: str, content_type: str) -> None:
        """
        Store bytes under a key that must not already hold something else.

        Re-writing identical bytes is a no-op, so a retried request is safe;
        writing different bytes is refused, which is what makes a stored
        version immutable.
        """
        self.ensure_bucket()
        existing = self._existing_sha(key)
        if existing is not None:
            if existing == sha256:
                return
            raise ImmutableObjectError(f"{key} already exists with different content")
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
            Metadata={"sha256": sha256},
        )

    def get(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError(f"Could not read {key}: {exc}") from exc

    def get_json(self, key: str) -> Dict[str, Any]:
        return json.loads(self.get(key).decode("utf-8"))
