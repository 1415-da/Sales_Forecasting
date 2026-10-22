"""
Safe inspection of an uploaded package: files, ZIP members, headers.

Everything uploaded is untrusted. This module:

* expands ZIP archives under explicit limits (member count, per-file size,
  total expanded size, compression ratio), refusing path traversal, absolute
  paths, symlinks, encryption, nested archives and executable or serialized
  content, and extracting under generated names -- never a member's own path;
* ignores OS metadata (``__MACOSX/``, ``.DS_Store``, ...) through an explicit
  allowlist, and allows one enclosing directory;
* checks each data file's *content* (Parquet magic bytes, decodable text, no
  pickle or executable signatures), not just its extension;
* reads only headers here. Rows are parsed by the adapter, once.
"""

from __future__ import annotations

import io
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from data_ingestion.limits import UploadLimits
from data_ingestion.normalization import normalize_columns
from training.contract import Issue

DATA_EXTENSIONS = {".csv": "csv", ".parquet": "parquet", ".pq": "parquet"}
ARCHIVE_EXTENSIONS = {".zip"}
NESTED_ARCHIVE_EXTENSIONS = {".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar"}
FORBIDDEN_EXTENSIONS = {
    ".pkl", ".pickle", ".joblib", ".npy", ".npz", ".h5", ".pt", ".pth", ".onnx",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".sh", ".bat", ".cmd", ".ps1", ".py", ".jar",
}
OS_METADATA_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
#: Text files commonly shipped with datasets; listed as excluded, never parsed.
IGNORED_TEXT_EXTENSIONS = {".txt", ".md", ".pdf", ".json", ".xml", ".html"}

SNIFF_BYTES = 64 * 1024
_PICKLE = re.compile(rb"^\x80[\x02-\x05]")


@dataclass
class StagedFile:
    """An uploaded file already written to local staging."""

    original_name: str
    path: str
    size: int
    sha256: str


@dataclass
class DataFile:
    """A tabular file the adapters may read."""

    name: str  # display name (basename), never used as a path
    path: str  # generated local staging path
    format: str
    size: int
    sha256: Optional[str]
    origin: str  # "upload" or "zip:<archive>"
    columns: List[str] = field(default_factory=list)
    normalized: Dict[str, str] = field(default_factory=dict)  # normalized -> raw
    ambiguous: Dict[str, List[str]] = field(default_factory=dict)
    rows_estimate: Optional[int] = None

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "format": self.format,
            "size": self.size,
            "sha256": self.sha256,
            "origin": self.origin,
            "columns": self.columns,
        }


@dataclass
class Package:
    files: List[DataFile] = field(default_factory=list)
    excluded: List[Dict[str, str]] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def by_name(self, name: str) -> Optional[DataFile]:
        return next((item for item in self.files if item.name == name), None)


def _ext(name: str) -> str:
    return os.path.splitext(name.lower())[1]


def _unsafe(message: str, file: Optional[str] = None, action: Optional[str] = None) -> Issue:
    return Issue("UNSAFE_ARCHIVE", message, stage="file", file=file, action=action)


def _content_issue(path: str, name: str, fmt: str) -> Optional[Issue]:
    """Refuse content that is not what the extension claims."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        head = handle.read(SNIFF_BYTES)
        if fmt == "parquet":
            handle.seek(max(0, size - 4))
            tail = handle.read(4)
    if _PICKLE.match(head) or head[:4] in (b"\x7fELF",) or head[:2] == b"MZ":
        return Issue(
            "UNSUPPORTED_FILE_TYPE",
            f"{name} contains serialized or executable content, not a data table.",
            stage="file",
            file=name,
            action="Upload CSV or Parquet data only. Model files are never accepted.",
        )
    if fmt == "parquet":
        if head[:4] != b"PAR1" or tail != b"PAR1":
            return Issue(
                "UNSUPPORTED_FILE_TYPE",
                f"{name} has a Parquet name but is not a Parquet file.",
                stage="file",
                file=name,
            )
        return None
    if head[:4] == b"PAR1" or head[:2] == b"PK":
        return Issue(
            "UNSUPPORTED_FILE_TYPE",
            f"{name} has a .csv name but contains binary data.",
            stage="file",
            file=name,
        )
    if b"\x00" in head:
        return Issue("UNSUPPORTED_FILE_TYPE", f"{name} is not a text CSV file.", stage="file", file=name)
    try:
        head.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        # A cut multi-byte character at the sniff boundary is not an error.
        if exc.start < len(head) - 4:
            return Issue(
                "UNSUPPORTED_FILE_TYPE",
                f"{name} is not UTF-8 text.",
                stage="file",
                file=name,
                action="Save the CSV as UTF-8.",
            )
    return None


def _read_header(data: DataFile) -> Optional[Issue]:
    try:
        if data.format == "parquet":
            import pyarrow.parquet as pq

            meta = pq.ParquetFile(data.path)
            data.columns = [str(name) for name in meta.schema_arrow.names]
            data.rows_estimate = meta.metadata.num_rows
        else:
            header = pd.read_csv(data.path, nrows=0, encoding="utf-8-sig")
            data.columns = [str(name) for name in header.columns]
    except Exception as exc:  # noqa: BLE001 - parse failure is a data issue
        return Issue(
            "UNSUPPORTED_FILE_TYPE",
            f"{data.name} could not be read as {data.format.upper()}: {type(exc).__name__}.",
            stage="file",
            file=data.name,
        )
    if not data.columns:
        return Issue("EMPTY_DATASET", f"{data.name} has no columns.", stage="file", file=data.name)
    data.normalized, data.ambiguous = normalize_columns(data.columns)
    if data.ambiguous:
        return Issue(
            "AMBIGUOUS_MAPPING",
            f"{data.name} has columns that become identical after trimming and case-folding: "
            + "; ".join(" / ".join(group) for group in data.ambiguous.values()),
            stage="file",
            file=data.name,
            action="Rename or remove the duplicate columns.",
        )
    return None


def _expand_zip(staged: StagedFile, workdir: str, limits: UploadLimits, package: Package, budget: Dict[str, int]) -> None:
    archive = staged.original_name
    try:
        handle = zipfile.ZipFile(staged.path)
    except zipfile.BadZipFile:
        package.issues.append(
            Issue("UNSUPPORTED_FILE_TYPE", f"{archive} is not a valid ZIP archive.", stage="file", file=archive)
        )
        return
    with handle:
        members = handle.infolist()
        data_members = []
        for info in members:
            name = info.filename.replace("\\", "/")
            parts = [part for part in name.split("/") if part]
            if not parts:
                continue
            if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts:
                package.issues.append(_unsafe(f"{archive} contains an unsafe path: {name[:120]}", archive))
                return
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                package.issues.append(_unsafe(f"{archive} contains a symbolic link: {name[:120]}", archive))
                return
            if info.flag_bits & 0x1:
                package.issues.append(_unsafe(f"{archive} contains an encrypted member.", archive))
                return
            if info.is_dir():
                continue
            base = parts[-1]
            if "__MACOSX" in parts or base.lower() in OS_METADATA_NAMES or base.startswith("._"):
                package.excluded.append({"name": base, "origin": f"zip:{archive}", "reason": "operating-system metadata"})
                continue
            ext = _ext(base)
            if ext in NESTED_ARCHIVE_EXTENSIONS:
                package.issues.append(_unsafe(f"{archive} contains a nested archive ({base}); nested archives are not supported.", archive))
                return
            if ext in FORBIDDEN_EXTENSIONS:
                package.issues.append(
                    _unsafe(f"{archive} contains {base}, which is executable or serialized content.", archive,
                            "Remove code, model and pickle files from the archive.")
                )
                return
            if len(parts) > 2:
                package.issues.append(
                    Issue(
                        "UNSUPPORTED_ARCHIVE_LAYOUT",
                        f"{archive}: {name[:120]} is nested more than one directory deep.",
                        stage="file",
                        file=archive,
                        action="Put the data files at the top of the archive or inside one dataset folder.",
                    )
                )
                return
            if ext not in DATA_EXTENSIONS:
                package.excluded.append({"name": base, "origin": f"zip:{archive}", "reason": "not a CSV or Parquet file"})
                continue
            if info.file_size > limits.max_file_bytes:
                package.issues.append(
                    Issue("UPLOAD_TOO_LARGE", f"{base} expands to {info.file_size:,} bytes; the per-file limit is {limits.max_file_bytes:,}.", stage="file", file=base)
                )
                return
            ratio = info.file_size / max(1, info.compress_size)
            if info.file_size > 1024 * 1024 and ratio > limits.max_compression_ratio:
                package.issues.append(_unsafe(f"{base} has a compression ratio of {ratio:.0f}:1 (limit {limits.max_compression_ratio:.0f}:1).", archive))
                return
            data_members.append((info, base, ext))

        for index, (info, base, ext) in enumerate(data_members):
            target = os.path.join(workdir, f"zip-{len(budget)}-{index}{ext}")
            budget[target] = 0
            written = 0
            with handle.open(info) as source, open(target, "wb") as sink:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    budget["expanded"] = budget.get("expanded", 0) + len(chunk)
                    if written > limits.max_file_bytes or budget["expanded"] > limits.max_expanded_bytes:
                        package.issues.append(
                            _unsafe(f"{archive} expands beyond the {limits.max_expanded_bytes:,}-byte limit.", archive)
                        )
                        return
                    sink.write(chunk)
            package.files.append(
                DataFile(name=base, path=target, format=DATA_EXTENSIONS[ext], size=written, sha256=None, origin=f"zip:{archive}")
            )


def inspect_package(staged: Sequence[StagedFile], workdir: str, limits: UploadLimits) -> Package:
    """Expand and check every uploaded file; read data-file headers."""
    package = Package()
    budget: Dict[str, int] = {}
    if not staged:
        package.issues.append(Issue("EMPTY_DATASET", "No file was uploaded.", stage="file"))
        return package
    for item in staged:
        name = os.path.basename(item.original_name.replace("\\", "/")) or "upload"
        ext = _ext(name)
        if item.size > limits.max_file_bytes:
            package.issues.append(
                Issue("UPLOAD_TOO_LARGE", f"{name} is {item.size:,} bytes; the per-file limit is {limits.max_file_bytes:,}.", stage="file", file=name)
            )
            continue
        if ext in ARCHIVE_EXTENSIONS:
            _expand_zip(StagedFile(name, item.path, item.size, item.sha256), workdir, limits, package, budget)
            continue
        if ext in FORBIDDEN_EXTENSIONS:
            package.issues.append(
                Issue("UNSUPPORTED_FILE_TYPE", f"{name}: model, pickle and executable files are never accepted.", stage="file", file=name)
            )
            continue
        if ext not in DATA_EXTENSIONS:
            package.issues.append(
                Issue("UNSUPPORTED_FILE_TYPE", f"{name}: only .csv, .parquet and .zip files are accepted.", stage="file", file=name)
            )
            continue
        package.files.append(
            DataFile(name=name, path=item.path, format=DATA_EXTENSIONS[ext], size=item.size, sha256=item.sha256, origin="upload")
        )

    if len(package.files) > limits.max_files:
        package.issues.append(
            Issue("TOO_MANY_FILES", f"{len(package.files)} data files; the limit is {limits.max_files}.", stage="file")
        )
    seen: Dict[str, str] = {}
    for data in package.files:
        key = data.name.lower()
        if key in seen:
            package.issues.append(
                Issue(
                    "AMBIGUOUS_FILES",
                    f"Two files are named {data.name} ({seen[key]} and {data.origin}).",
                    stage="file",
                    file=data.name,
                    action="Upload each file once.",
                )
            )
        seen[key] = data.origin
    if not package.ok:
        return package
    for data in package.files:
        issue = _content_issue(data.path, data.name, data.format) or _read_header(data)
        if issue:
            package.issues.append(issue)
    if not package.files and package.ok:
        package.issues.append(Issue("EMPTY_DATASET", "The upload contains no CSV or Parquet data file.", stage="file"))
    return package
