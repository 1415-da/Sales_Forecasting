"""
The adapter interface.

An adapter turns one known *source format* into the canonical store_day_v1
frame. It has two jobs, kept separate on purpose:

* ``detect`` reports, with evidence, whether a package looks like its source,
  which files and fields it would use, what is missing or ambiguous, and what
  the user must confirm. It decides nothing.
* ``normalize`` applies a *confirmed* configuration and returns the canonical
  frame (for the strict validator), the retained-but-unused context, and a
  transformation report explaining every mapping and every exclusion.

Match categories are evidence-based, not scores:

* ``exact``   -- every required field plus the source's characteristic fields;
* ``partial`` -- the required fields only; could be this source or another;
* ``manual``  -- usable only through an explicit, confirmed mapping;
* ``none``    -- cannot read this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from data_ingestion.package_inspector import DataFile, Package
from training.contract import FeatureOptions, Issue

MATCH_ORDER = ("exact", "partial", "manual", "none")


@dataclass
class Confirmation:
    """Something the user must state before normalization can run."""

    key: str
    label: str
    options: List[Dict[str, str]] = field(default_factory=list)
    required: bool = True
    help: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "options": self.options, "required": self.required, "help": self.help}


@dataclass
class Detection:
    adapter_id: str
    adapter_version: str
    label: str
    match: str
    evidence: List[str] = field(default_factory=list)
    files_used: List[Dict[str, str]] = field(default_factory=list)
    files_excluded: List[Dict[str, str]] = field(default_factory=list)
    missing_files: List[str] = field(default_factory=list)
    missing_fields: List[str] = field(default_factory=list)
    ambiguities: List[str] = field(default_factory=list)
    confirmations: List[Confirmation] = field(default_factory=list)
    proposed_mapping: Dict[str, Any] = field(default_factory=dict)
    columns: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.match in ("exact", "partial", "manual") and not self.missing_files and not self.ambiguities

    def as_dict(self) -> Dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "label": self.label,
            "match": self.match,
            "evidence": self.evidence,
            "files_used": self.files_used,
            "files_excluded": self.files_excluded,
            "missing_files": self.missing_files,
            "missing_fields": self.missing_fields,
            "ambiguities": self.ambiguities,
            "confirmations": [item.as_dict() for item in self.confirmations],
            "proposed_mapping": self.proposed_mapping,
            "columns": self.columns,
            "usable": self.usable,
        }


@dataclass
class Normalized:
    """What an adapter hands to the strict validator."""

    #: Canonical column names only, values as text, plus ``_source_row``.
    frame: Optional[pd.DataFrame]
    options: FeatureOptions
    source_namespace: str
    target_type: str
    source_grain: str
    #: Retained source fields not used by store_day_v1 (keyed by store_id, date).
    context: Optional[pd.DataFrame] = None
    transformations: List[Dict[str, str]] = field(default_factory=list)
    retained_columns: List[Dict[str, str]] = field(default_factory=list)
    excluded_files: List[Dict[str, str]] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    message: Optional[str] = None
    store_metadata: Optional[Dict[str, Any]] = None
    rows_received: int = 0
    mapping: Dict[str, Any] = field(default_factory=dict)


class AdapterConfigError(ValueError):
    """The supplied configuration is incomplete; detection lists what is needed."""


class Adapter:
    id: str = ""
    version: str = "1"
    label: str = ""

    def detect(self, package: Package) -> Detection:  # pragma: no cover - interface
        raise NotImplementedError

    def normalize(self, package: Package, config: Dict[str, Any], max_rows: int) -> Normalized:  # pragma: no cover
        raise NotImplementedError

    # -- helpers shared by adapters ----------------------------------------

    @staticmethod
    def read_text_table(data: DataFile, columns: List[str], max_rows: int) -> pd.DataFrame:
        """
        Read only the needed raw columns, all as text.

        Text, not inferred types: an integer store ``1`` must stay ``"1"``,
        never become ``1.0`` because a blank elsewhere forced a float column.
        One extra row is read so an over-limit file is detected, not
        truncated silently.
        """
        if data.format == "parquet":
            frame = pd.read_parquet(data.path, columns=columns)
            if len(frame) > max_rows:
                frame = frame.iloc[: max_rows + 1]
            for column in frame.columns:
                if pd.api.types.is_datetime64_any_dtype(frame[column]):
                    frame[column] = frame[column].dt.strftime("%Y-%m-%d")
                elif pd.api.types.is_integer_dtype(frame[column]):
                    frame[column] = frame[column].astype("Int64").astype("string")
                else:
                    frame[column] = frame[column].astype("string")
            return frame
        return pd.read_csv(
            data.path,
            usecols=columns,
            dtype=str,
            keep_default_na=False,
            na_values=[""],
            encoding="utf-8-sig",
            nrows=max_rows + 1,
        )

    @staticmethod
    def row_limit_issue(frame: pd.DataFrame, max_rows: int, file: str) -> Optional[Issue]:
        if len(frame) > max_rows:
            return Issue(
                "UPLOAD_TOO_LARGE",
                f"{file} has more than {max_rows:,} rows, the processing limit.",
                stage="file",
                file=file,
                action="Split the history into smaller uploads and use append mode.",
            )
        return None


def currency_confirmation() -> Confirmation:
    return Confirmation(
        key="currency",
        label="Currency of the sales figures",
        help=(
            "Confirm the currency the revenue is recorded in, or choose UNKNOWN. "
            "It is never inferred from a country. Only data in the serving currency "
            "can replace the deployed model."
        ),
    )


MISSING_DATES_CONFIRMATION = Confirmation(
    key="missing_dates",
    label="Stores with days missing inside their history",
    options=[
        {"value": "reject", "label": "Reject the dataset (default)"},
        {"value": "exclude_stores", "label": "Exclude those stores and list them"},
    ],
    required=False,
    help="Lag features need every day. Excluded stores cannot be forecast by the resulting model.",
)
