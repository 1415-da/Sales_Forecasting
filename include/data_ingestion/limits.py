"""
Upload and processing limits, in one place.

Every layer reads these: the API route (request size, per-file size, file
count), the package inspector (ZIP expansion), the processing worker (rows,
time, concurrency), and the browser (shown on the upload card, checked before
sending). Defaults are sized for this deployment's 8 GB Docker VM: the largest
realistic single source here is Rossmann's 38 MB / 1.0 M-row train.csv, which
validates in seconds and occupies under 1 GB while parsed.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

MB = 1024 * 1024


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class UploadLimits:
    #: Whole request (all files together).
    max_upload_bytes: int = 200 * MB
    #: Any one file, uploaded or extracted from a ZIP.
    max_file_bytes: int = 150 * MB
    #: Files per upload, counting ZIP members.
    max_files: int = 10
    #: Total uncompressed bytes a ZIP may expand to.
    max_expanded_bytes: int = 500 * MB
    #: Uncompressed / compressed, per ZIP member. Real CSVs compress ~5-15x;
    #: far beyond that is a decompression bomb.
    max_compression_ratio: float = 100.0
    #: Rows after normalization.
    max_rows: int = 5_000_000
    #: Wall-clock budget for one processing job.
    processing_timeout_seconds: int = 900
    #: Concurrent processing jobs per API process.
    max_concurrent_jobs: int = 2
    #: Rows in the bounded preview.
    preview_rows: int = 20
    #: Uploads at or below this size are processed inside the request.
    inline_processing_bytes: int = 2 * MB

    @classmethod
    def from_env(cls) -> "UploadLimits":
        return cls(
            max_upload_bytes=_int("TRAINING_UPLOAD_MAX_BYTES", cls.max_upload_bytes),
            max_file_bytes=_int("TRAINING_UPLOAD_MAX_FILE_BYTES", cls.max_file_bytes),
            max_files=_int("TRAINING_UPLOAD_MAX_FILES", cls.max_files),
            max_expanded_bytes=_int("TRAINING_UPLOAD_MAX_EXPANDED_BYTES", cls.max_expanded_bytes),
            max_compression_ratio=_float("TRAINING_UPLOAD_MAX_COMPRESSION_RATIO", cls.max_compression_ratio),
            max_rows=_int("TRAINING_UPLOAD_MAX_ROWS", cls.max_rows),
            processing_timeout_seconds=_int("TRAINING_PROCESSING_TIMEOUT_SECONDS", cls.processing_timeout_seconds),
            max_concurrent_jobs=max(1, _int("TRAINING_MAX_CONCURRENT_JOBS", cls.max_concurrent_jobs)),
            preview_rows=_int("TRAINING_PREVIEW_ROWS", cls.preview_rows),
            inline_processing_bytes=_int("TRAINING_INLINE_PROCESSING_BYTES", cls.inline_processing_bytes),
        )

    def as_dict(self) -> dict:
        return asdict(self)
