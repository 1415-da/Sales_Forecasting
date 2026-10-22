"""
The store-day training contract: schema, validation, hashing, and combination.

A training dataset is **store-day revenue**: one row per store per calendar
day, with ``sales`` the day's revenue. That is the grain the model is trained
and served at. This module is the *strict canonical validator*: it only ever
sees canonical columns. Source formats (Rossmann, differently named columns,
multi-file packages) are translated by ``data_ingestion`` adapters first;
anything an adapter does not map never reaches this module.

This is not the history sent with a single forecast request (a short series
the API never stores) and not the actual-sales feed monitoring scores against
(the ``actuals`` table). Those are separate workflows with their own rules.

Nothing in here touches storage or a database: every function takes a frame
and a policy and returns a result, so the API and the DAG run the identical
check and the tests exercise it without services.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

SCHEMA_VERSION = "store_day_v1"
TARGET = "sales"
TARGET_TYPE = "revenue"
TARGET_DESCRIPTION = "Total daily revenue of one store, in the dataset currency."
FREQUENCY = "D"
SOURCE_GRAIN = "store_day"

REQUIRED_COLUMNS: Tuple[str, ...] = ("date", "store_id", "sales")

#: Optional context columns and the model feature each one becomes. Nothing
#: outside this mapping reaches the canonical table.
CONTEXT_COLUMNS: Dict[str, str] = {
    "snap_day": "1 on a day SNAP benefits are issued in the store's state, else 0.",
    "is_closed": "1 on a day the store was closed, else 0.",
}

#: Columns that mean the table is at product grain, not store-day grain.
PRODUCT_GRAIN_COLUMNS: Tuple[str, ...] = (
    "product_id",
    "item_id",
    "sku",
    "dept_id",
    "cat_id",
    "quantity_sold",
)

SOURCE_TYPES: Tuple[str, ...] = ("real", "synthetic")
CONFLICT_POLICIES: Tuple[str, ...] = ("reject", "prefer_new")

#: How each derivable context feature is produced when the data has no column.
SNAP_POLICIES: Tuple[str, ...] = ("derive_us_state_schedule", "omit")
#: ``source`` = taken from the data's own opening status (e.g. Rossmann Open).
CLOSURE_POLICIES: Tuple[str, ...] = ("derive_platform_calendar", "omit", "source")
#: What to do with stores that have days missing inside their history.
MISSING_DATE_POLICIES: Tuple[str, ...] = ("reject", "exclude_stores")

STORE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
#: Explicitly declared as not known. Allowed for training; never deployable to
#: a model that serves revenue in a known currency.
UNKNOWN_CURRENCY = "UNKNOWN"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: SNAP issue days per state, identical to the serving feature builder's table
#: (``api.services.feature_service.SNAP_DAYS_BY_STATE``; a test pins the two).
SNAP_DAYS_BY_STATE: Dict[str, frozenset] = {
    "CA": frozenset(range(1, 11)),
    "TX": frozenset({1, 3, 5, 6, 7, 9, 11, 12, 13, 15}),
    "WI": frozenset({2, 3, 5, 6, 8, 9, 11, 12, 14, 15}),
}

#: The platform's closure calendar, which serving also applies.
CLOSED_MONTH_DAY = (12, 25)

#: Rows shown in a validation preview.
PREVIEW_ROWS = 10
#: Examples listed under each issue.
MAX_EXAMPLES = 10

DEFAULT_ML_CONFIG = "/usr/local/airflow/include/config/ml_config.yaml"

#: Validation stages, in the order they run.
STAGES = (
    "file",
    "detection",
    "mapping",
    "schema",
    "rows",
    "readiness",
    "append",
    "deployment",
)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_list(name: str) -> Optional[List[str]]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def feature_warmup_days(config_path: Optional[str] = None) -> int:
    """
    Days of history before a row's lag and rolling features are all observed.

    Read from the feature configuration rather than restated, so lengthening a
    lag in ``ml_config.yaml`` raises the minimum history automatically.
    """
    import yaml

    candidates = [
        config_path,
        os.getenv("ML_CONFIG_PATH"),
        DEFAULT_ML_CONFIG,
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "ml_config.yaml"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                features = (yaml.safe_load(handle) or {}).get("features") or {}
            lags = [int(value) for value in features.get("lag_features") or []]
            windows = [
                int(value)
                for value in (features.get("rolling_features") or {}).get("windows") or []
            ]
            return max(lags + windows + [1])
    return 30


@dataclass(frozen=True)
class ValidationPolicy:
    """
    Everything that decides whether a dataset is trainable.

    Environment-configurable because file limits, the returns policy, and the
    authorised store scope belong to the deployment, not to the code.
    ``allowed_currencies`` is the currency the *serving* model uses: a dataset
    in another currency can be trained, but it is a different target and is
    refused at deployment.
    """

    max_bytes: int = 200 * 1024 * 1024
    max_rows: int = 5_000_000
    #: ``reject`` refuses negative daily revenue; ``allow`` keeps it (net of
    #: returns) and reports it as a warning.
    negative_sales: str = "reject"
    #: Store identifiers this deployment may train on; ``None`` means any
    #: well-formed identifier.
    allowed_stores: Optional[Tuple[str, ...]] = None
    allowed_currencies: Tuple[str, ...] = ("USD",)
    warmup_days: int = 30
    #: Observed training days a store needs after its warm-up.
    min_train_days_after_warmup: int = 30
    validation_fraction: float = 0.1
    test_fraction: float = 0.2
    min_validation_days: int = 14
    min_test_days: int = 28

    @property
    def min_store_days(self) -> int:
        """Shortest per-store history that can yield usable training rows."""
        return self.warmup_days + self.min_train_days_after_warmup

    @property
    def min_total_dates(self) -> int:
        """Distinct dates needed for train, validation, and test windows."""
        by_validation = math.ceil(self.min_validation_days / self.validation_fraction)
        by_test = math.ceil(self.min_test_days / self.test_fraction)
        return max(by_validation, by_test, self.min_store_days + self.min_test_days)

    @classmethod
    def from_env(cls) -> "ValidationPolicy":
        from data_ingestion.limits import UploadLimits

        allowed = _env_list("TRAINING_ALLOWED_STORES")
        currencies = _env_list("TRAINING_ALLOWED_CURRENCIES") or ["USD"]
        negative = os.getenv("TRAINING_NEGATIVE_SALES_POLICY", "reject").strip().lower()
        limits = UploadLimits.from_env()
        return cls(
            max_bytes=limits.max_upload_bytes,
            max_rows=limits.max_rows,
            negative_sales=negative if negative in ("reject", "allow") else "reject",
            allowed_stores=tuple(allowed) if allowed else None,
            allowed_currencies=tuple(value.upper() for value in currencies),
            warmup_days=feature_warmup_days(),
            min_train_days_after_warmup=_env_int("TRAINING_MIN_TRAIN_DAYS", 30),
            min_validation_days=_env_int("TRAINING_MIN_VALIDATION_DAYS", 14),
            min_test_days=_env_int("TRAINING_MIN_TEST_DAYS", 28),
        )

    def describe(self) -> Dict[str, Any]:
        """The policy as data, for the contract endpoint and the report."""
        return {
            "max_bytes": self.max_bytes,
            "max_rows": self.max_rows,
            "negative_sales": self.negative_sales,
            "allowed_stores": list(self.allowed_stores) if self.allowed_stores else None,
            "allowed_currencies": list(self.allowed_currencies),
            "feature_warmup_days": self.warmup_days,
            "min_store_days": self.min_store_days,
            "min_total_dates": self.min_total_dates,
            "validation_fraction": self.validation_fraction,
            "test_fraction": self.test_fraction,
            "min_validation_days": self.min_validation_days,
            "min_test_days": self.min_test_days,
        }


@dataclass(frozen=True)
class FeatureOptions:
    """
    Per-dataset choices for context features and gaps, recorded in the
    manifest so the DAG re-validates under exactly the same rules.
    """

    snap: str = "derive_us_state_schedule"
    closure: str = "derive_platform_calendar"
    missing_dates: str = "reject"
    #: Which public-holiday calendar the feature pipeline may use for
    #: ``is_holiday``: the US federal calendar (the platform's M5 data) or none.
    #: A US calendar is never applied to a non-US source.
    holiday_calendar: str = "us_federal"

    def as_dict(self) -> Dict[str, str]:
        return {
            "snap": self.snap,
            "closure": self.closure,
            "missing_dates": self.missing_dates,
            "holiday_calendar": self.holiday_calendar,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "FeatureOptions":
        data = data or {}
        return cls(
            snap=data.get("snap", cls.snap),
            closure=data.get("closure", cls.closure),
            missing_dates=data.get("missing_dates", cls.missing_dates),
            holiday_calendar=data.get("holiday_calendar", cls.holiday_calendar),
        )


def contract_description(policy: ValidationPolicy) -> Dict[str, Any]:
    """The accepted canonical schema, as shown on the upload card."""
    return {
        "schema_version": SCHEMA_VERSION,
        "grain": "One row per store per calendar day.",
        "target": TARGET,
        "target_type": TARGET_TYPE,
        "target_description": TARGET_DESCRIPTION,
        "frequency": FREQUENCY,
        "required_columns": [
            {"name": "date", "type": "date", "description": "Calendar day, YYYY-MM-DD."},
            {
                "name": "store_id",
                "type": "string",
                "description": "Store identifier: letters, digits, '_' or '-', up to 64 characters.",
            },
            {"name": "sales", "type": "number", "description": TARGET_DESCRIPTION},
        ],
        "optional_columns": [
            {"name": name, "type": "0 or 1", "description": description}
            for name, description in CONTEXT_COLUMNS.items()
        ],
        "derivations": {
            "snap_day": (
                "Canonical uploads without the column: derived from the US state SNAP schedule "
                "for CA_, TX_ and WI_ stores; otherwise left out of training. Other sources "
                "(e.g. Rossmann) never derive SNAP."
            ),
            "is_closed": (
                "Canonical uploads without the column: derived from the platform closure "
                "calendar (25 December). An explicit opening status from the source always "
                "takes precedence; independent sources must choose a closure policy."
            ),
        },
        "rejected": (
            "Product-level or transaction-level rows (product_id, item_id, sku, "
            "quantity_sold, ...) and quantity targets. Aggregate to store-day revenue first."
        ),
        "file_types": ["csv", "parquet", "zip"],
        "policy": policy.describe(),
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class UploadReadError(ValueError):
    """The bytes could not be read as a table at all."""


def detect_format(filename: str, content: bytes) -> str:
    """
    ``csv`` or ``parquet``, from the name and the bytes.

    The magic number is checked as well as the extension: a renamed file is
    refused instead of being parsed as something it is not.
    """
    name = (filename or "").lower()
    is_parquet_bytes = content[:4] == b"PAR1" and content[-4:] == b"PAR1"
    if name.endswith(".parquet") or name.endswith(".pq"):
        if not is_parquet_bytes:
            raise UploadReadError("The file has a .parquet name but is not a Parquet file.")
        return "parquet"
    if name.endswith(".csv"):
        if is_parquet_bytes:
            raise UploadReadError("The file has a .csv name but contains Parquet data.")
        return "csv"
    raise UploadReadError("Only .csv and .parquet files are accepted.")


def read_table(content: bytes, file_format: str) -> pd.DataFrame:
    """
    Parse a canonical file into a frame of raw values.

    CSV cells are read as text, so parsing (dates, numbers) happens in
    ``validate`` where each failure can be reported with its row number.
    Column names are stripped and lower-cased; adapters handle anything else.
    """
    try:
        if file_format == "parquet":
            frame = pd.read_parquet(io.BytesIO(content))
        else:
            frame = pd.read_csv(
                io.BytesIO(content), dtype=str, keep_default_na=False, na_values=[""]
            )
    except Exception as exc:  # noqa: BLE001 - parser errors become one message
        raise UploadReadError(f"The file could not be read as {file_format.upper()}: {exc}") from exc
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    return frame


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    """One problem, with enough detail to fix it."""

    code: str
    message: str
    count: int = 0
    examples: List[Dict[str, Any]] = field(default_factory=list)
    severity: str = "error"
    stage: str = "rows"
    column: Optional[str] = None
    file: Optional[str] = None
    action: Optional[str] = None
    #: A warning the user must acknowledge before the version can be sealed.
    requires_ack: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "stage": self.stage,
            "file": self.file,
            "column": self.column,
            "message": self.message,
            "action": self.action,
            "count": self.count,
            "examples": self.examples[:MAX_EXAMPLES],
            "requires_ack": self.requires_ack,
        }


def warning(code: str, message: str, **kwargs: Any) -> Issue:
    return Issue(code, message, severity="warning", **kwargs)


@dataclass
class ValidationResult:
    """The report, and the clean frame when there were no errors."""

    report: Dict[str, Any]
    frame: Optional[pd.DataFrame]

    @property
    def is_valid(self) -> bool:
        return self.frame is not None


def _row_examples(raw: pd.DataFrame, mask: pd.Series, columns: Sequence[str]) -> List[Dict[str, Any]]:
    """Up to ``MAX_EXAMPLES`` offending rows, numbered as in the file."""
    row_numbers = raw["_source_row"] if "_source_row" in raw.columns else None
    examples = []
    for index in raw.index[mask][:MAX_EXAMPLES]:
        number = int(row_numbers.at[index]) if row_numbers is not None else int(index) + 2
        example: Dict[str, Any] = {"row": number}
        for column in columns:
            if column in raw.columns:
                value = raw.at[index, column]
                example[column] = None if pd.isna(value) else str(value)[:64]
        examples.append(example)
    return examples


def _parse_dates(series: pd.Series) -> pd.Series:
    """Strict calendar dates; anything else becomes NaT."""
    if pd.api.types.is_datetime64_any_dtype(series):
        parsed = pd.to_datetime(series, errors="coerce")
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_localize(None)
        has_time = parsed.notna() & (parsed != parsed.dt.normalize())
        return parsed.mask(has_time)
    if series.map(lambda value: isinstance(value, date)).all():
        return pd.to_datetime(series, errors="coerce")
    text = series.astype("string").str.strip()
    well_formed = text.str.match(DATE_PATTERN.pattern).fillna(False)
    return pd.to_datetime(text.where(well_formed), format="%Y-%m-%d", errors="coerce")


def _parse_flags(series: pd.Series) -> pd.Series:
    """0/1 flags; accepts true/false. Anything else becomes NaN."""
    mapping = {"1": 1.0, "0": 0.0, "true": 1.0, "false": 0.0, "1.0": 1.0, "0.0": 0.0}
    text = series.astype("string").str.strip().str.lower()
    return text.map(mapping).astype("float64")


def snap_flag(store_id: str, day: pd.Timestamp) -> Optional[int]:
    """SNAP flag from the state schedule, or ``None`` for an unknown state."""
    days = SNAP_DAYS_BY_STATE.get(str(store_id)[:2].upper())
    if days is None or str(store_id)[2:3] not in ("_", "-"):
        return None
    return 1 if day.day in days else 0


def split_dates(dates: Sequence[pd.Timestamp], policy: ValidationPolicy) -> Dict[str, Any]:
    """
    Chronological split by calendar date, shared by validation and training.

    Splitting by date rather than by row position keeps one day's stores
    together: a position split can put CA_1's 1 March in training and TX_1's 1
    March in test, which leaks the day into evaluation.
    """
    ordered = sorted(pd.Timestamp(value) for value in set(dates))
    total = len(ordered)
    test_days = max(policy.min_test_days, math.ceil(total * policy.test_fraction))
    validation_days = max(
        policy.min_validation_days, math.ceil(total * policy.validation_fraction)
    )
    train_days = total - test_days - validation_days
    if train_days <= 0:
        return {"valid": False, "total_dates": total}
    train = ordered[:train_days]
    validation = ordered[train_days : train_days + validation_days]
    test = ordered[train_days + validation_days :]
    window = lambda items: [items[0].date().isoformat(), items[-1].date().isoformat()]  # noqa: E731
    return {
        "valid": True,
        "total_dates": total,
        "train": window(train),
        "validation": window(validation),
        "test": window(test),
        "train_days": len(train),
        "validation_days": len(validation),
        "test_days": len(test),
    }


def currency_issue(currency: str) -> Optional[Issue]:
    """A malformed currency declaration, or ``None``."""
    if currency == UNKNOWN_CURRENCY or CURRENCY_PATTERN.match(currency or ""):
        return None
    return Issue(
        "INVALID_CURRENCY",
        "Currency must be a 3-letter ISO code (e.g. USD, EUR) or explicitly UNKNOWN.",
        stage="mapping",
        action="Confirm the currency the sales figures are recorded in.",
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(
    raw: pd.DataFrame,
    policy: ValidationPolicy,
    *,
    currency: str,
    source_type: str,
    require_history: bool = True,
    options: Optional[FeatureOptions] = None,
    file: Optional[str] = None,
) -> ValidationResult:
    """
    Check a canonical frame against the contract and return a report.

    ``raw`` holds canonical column names only (an adapter has already mapped
    the source); an optional ``_source_row`` column carries the original row
    numbers for error references. ``require_history=False`` is for the upload
    half of an append: rows, grain and continuity are checked, but history
    length and the date split are judged on the combined dataset.

    Nothing is silently dropped or repaired. Exact duplicate store-days are
    collapsed and counted; stores with gaps are excluded only under the
    explicitly chosen ``exclude_stores`` policy, and listed.
    """
    options = options or FeatureOptions()
    errors: List[Issue] = []
    warnings: List[Issue] = []
    rows_received = int(len(raw))

    def finish(
        frame: Optional[pd.DataFrame],
        extra: Optional[Dict[str, Any]] = None,
        rows_checked: bool = False,
    ) -> ValidationResult:
        for issue in errors + warnings:
            issue.file = issue.file or file
        report: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "valid" if frame is not None and not errors else "invalid",
            "target": TARGET,
            "target_type": TARGET_TYPE,
            "frequency": FREQUENCY,
            "currency": currency,
            "source_type": source_type,
            "rows_received": rows_received,
            # Null, not zero, when row validation never ran: "0 invalid rows"
            # after a schema failure would claim a check that did not happen.
            "row_validation_performed": rows_checked,
            "rows_valid": None,
            "rows_invalid": None,
            "errors": [issue.as_dict() for issue in errors],
            "warnings": [issue.as_dict() for issue in warnings],
            "policy": policy.describe(),
            "feature_options": options.as_dict(),
        }
        report.update(extra or {})
        return ValidationResult(report=report, frame=frame if not errors else None)

    # -- declared metadata -------------------------------------------------
    bad_currency = currency_issue(currency)
    if bad_currency:
        errors.append(bad_currency)
    if source_type not in SOURCE_TYPES:
        errors.append(
            Issue("INVALID_SOURCE_TYPE", "Source type must be 'real' or 'synthetic'.", stage="mapping")
        )
    for name, value, allowed in (
        ("snap", options.snap, SNAP_POLICIES),
        ("closure", options.closure, CLOSURE_POLICIES),
        ("missing_dates", options.missing_dates, MISSING_DATE_POLICIES),
    ):
        if value not in allowed:
            errors.append(Issue("INVALID_POLICY", f"{name} policy must be one of {allowed}.", stage="mapping"))

    # -- shape -------------------------------------------------------------
    if rows_received == 0:
        errors.append(Issue("EMPTY_DATASET", "The data has no rows.", stage="file"))
        return finish(None)
    if rows_received > policy.max_rows:
        errors.append(
            Issue(
                "UPLOAD_TOO_LARGE",
                f"{rows_received:,} rows exceeds the limit of {policy.max_rows:,}.",
                stage="file",
                action="Split the history into smaller uploads and use append mode.",
            )
        )
        return finish(None)

    columns = [column for column in raw.columns if column != "_source_row"]
    duplicated_columns = sorted({column for column in columns if columns.count(column) > 1})
    if duplicated_columns:
        errors.append(
            Issue(
                "AMBIGUOUS_MAPPING",
                f"Columns appear more than once: {', '.join(duplicated_columns)}.",
                stage="schema",
                action="Keep one column per field.",
            )
        )
        return finish(None)

    product_columns = [column for column in columns if column in PRODUCT_GRAIN_COLUMNS]
    if product_columns:
        errors.append(
            Issue(
                "SOURCE_GRAIN_UNSUPPORTED",
                "This looks like product-level data (found "
                f"{', '.join(product_columns)}). Training data is store-day revenue.",
                stage="schema",
                action="Aggregate to one row per store per day with that day's revenue.",
            )
        )
    missing = [column for column in REQUIRED_COLUMNS if column not in columns]
    for column in missing:
        errors.append(
            Issue(
                "MISSING_REQUIRED_COLUMN",
                f"Required column '{column}' is missing.",
                stage="schema",
                column=column,
                action="Map a source column to it, or use an adapter that recognises the source.",
            )
        )
    unknown = [
        column
        for column in columns
        if column not in REQUIRED_COLUMNS
        and column not in CONTEXT_COLUMNS
        and column not in PRODUCT_GRAIN_COLUMNS
    ]
    if unknown:
        errors.append(
            Issue(
                "UNMAPPED_COLUMNS",
                f"Columns with no mapping to a model feature: {', '.join(unknown)}.",
                stage="schema",
                action=(
                    "Remove them, or upload through an adapter that retains them as context. "
                    f"The only optional canonical columns are {', '.join(CONTEXT_COLUMNS)}."
                ),
            )
        )
    if missing or product_columns or unknown:
        return finish(None)

    # -- values ------------------------------------------------------------
    parsed = pd.DataFrame(index=raw.index)
    parsed["date"] = _parse_dates(raw["date"])
    bad_date = parsed["date"].isna()
    if bad_date.any():
        errors.append(
            Issue(
                "INVALID_DATE_VALUES",
                "Dates must be calendar days written YYYY-MM-DD (no time of day).",
                int(bad_date.sum()),
                _row_examples(raw, bad_date, ["date"]),
                column="date",
                action="Fix the dates, or choose the source's date format in the mapping.",
            )
        )

    store_text = raw["store_id"].astype("string").str.strip()
    parsed["store_id"] = store_text
    bad_store = store_text.isna() | ~store_text.fillna("").str.match(STORE_ID_PATTERN.pattern)
    if bad_store.any():
        errors.append(
            Issue(
                "INVALID_STORE_ID",
                "store_id must be letters, digits, '_' or '-' (up to 64 characters) and not empty.",
                int(bad_store.sum()),
                _row_examples(raw, bad_store, ["store_id"]),
                column="store_id",
            )
        )
    if policy.allowed_stores is not None:
        outside = ~bad_store & ~store_text.isin(policy.allowed_stores)
        if outside.any():
            errors.append(
                Issue(
                    "STORE_OUT_OF_SCOPE",
                    "These stores are outside the store scope this deployment may train on.",
                    int(outside.sum()),
                    _row_examples(raw, outside, ["store_id"]),
                    column="store_id",
                    action="Remove them, or ask an administrator to extend TRAINING_ALLOWED_STORES.",
                )
            )

    sales = pd.to_numeric(raw["sales"], errors="coerce")
    blank_sales = raw["sales"].isna() | (raw["sales"].astype("string").str.strip() == "")
    if blank_sales.any():
        errors.append(
            Issue(
                "NULL_TARGET",
                "sales is empty. A day with no sales is 0, not blank.",
                int(blank_sales.sum()),
                _row_examples(raw, blank_sales, ["store_id", "date", "sales"]),
                column="sales",
            )
        )
    finite = sales.notna() & sales.abs().lt(float("inf"))
    not_numeric = ~blank_sales & ~finite
    if not_numeric.any():
        errors.append(
            Issue(
                "NON_NUMERIC_TARGET",
                "sales must be a finite number.",
                int(not_numeric.sum()),
                _row_examples(raw, not_numeric, ["store_id", "date", "sales"]),
                column="sales",
            )
        )
    parsed["sales"] = sales.astype("float64")
    negative = parsed["sales"] < 0
    if negative.any():
        issue = Issue(
            "NEGATIVE_TARGET",
            "Negative daily revenue."
            + (
                " Allowed by the returns policy (net of returns)."
                if policy.negative_sales == "allow"
                else " The returns policy rejects it."
            ),
            int(negative.sum()),
            _row_examples(raw, negative, ["store_id", "date", "sales"]),
            column="sales",
            action=None if policy.negative_sales == "allow" else "Correct the rows, or ask an administrator to allow net-of-returns revenue.",
        )
        if policy.negative_sales == "allow":
            issue.severity = "warning"
            warnings.append(issue)
        else:
            errors.append(issue)

    context: Dict[str, str] = {}
    for column in CONTEXT_COLUMNS:
        if column not in columns:
            continue
        flags = _parse_flags(raw[column])
        bad_flag = flags.isna()
        if bad_flag.any():
            errors.append(
                Issue(
                    "INVALID_CONTEXT_VALUE",
                    f"{column} must be 0 or 1 on every row; a partly filled column cannot be used.",
                    int(bad_flag.sum()),
                    _row_examples(raw, bad_flag, ["store_id", "date", column]),
                    column=column,
                )
            )
        parsed[column] = flags
        context[column] = "provided"

    row_ok = ~(bad_date | bad_store | blank_sales | not_numeric)
    counts = {"rows_valid": int(row_ok.sum()), "rows_invalid": int((~row_ok).sum())}
    if errors:
        valid_keys = parsed[row_ok]
        dup = valid_keys.duplicated(["store_id", "date"], keep=False)
        if dup.any():
            warnings.append(
                warning(
                    "DUPLICATES_NOT_CHECKED",
                    "Some store-days appear more than once; they are checked once the errors above are fixed.",
                    count=int(dup.sum()),
                )
            )
        result = finish(None, counts, rows_checked=True)
        result.report.update(counts)
        return result

    # -- grain: one row per store-day -------------------------------------
    frame = parsed.copy()
    frame["_row"] = raw["_source_row"].values if "_source_row" in raw.columns else frame.index + 2
    key = ["store_id", "date"]
    value_columns = ["sales"] + [column for column in CONTEXT_COLUMNS if column in frame.columns]
    exact = frame.duplicated(key + value_columns, keep="first")
    exact_removed = int(exact.sum())
    frame = frame[~exact]
    conflicts = frame.duplicated(key, keep=False)
    conflict_count = 0
    if conflicts.any():
        conflict_count = int(frame[conflicts].drop_duplicates(key).shape[0])
        examples = (
            frame[conflicts]
            .sort_values(key + ["_row"])
            .head(MAX_EXAMPLES)
            .assign(date=lambda f: f["date"].dt.date.astype(str))
            .rename(columns={"_row": "row"})[["row", "store_id", "date", "sales"]]
            .to_dict("records")
        )
        errors.append(
            Issue(
                "CONFLICTING_RECORDS",
                "The same store-day appears more than once with different values.",
                conflict_count,
                examples,
                action="Keep one row per store per day.",
            )
        )
    if exact_removed:
        warnings.append(
            warning(
                "DUPLICATE_KEYS",
                "Identical repeated rows were collapsed to one (first occurrence kept).",
                count=exact_removed,
            )
        )

    # -- context derivation -----------------------------------------------
    if "snap_day" not in frame.columns:
        if options.snap == "omit":
            context["snap_day"] = "not applicable to this source (omitted)"
        else:
            stores = frame["store_id"].unique()
            states = {store: snap_flag(store, pd.Timestamp("2000-01-01")) is not None for store in stores}
            if all(states.values()):
                schedule = frame["store_id"].str[:2].str.upper().map(
                    lambda state: SNAP_DAYS_BY_STATE.get(state, frozenset())
                )
                day = frame["date"].dt.day
                frame["snap_day"] = [float(d in days) for d, days in zip(day, schedule)]
                context["snap_day"] = "derived from the US state SNAP schedule"
            else:
                unknown_states = sorted(store for store, known in states.items() if not known)
                context["snap_day"] = "unavailable"
                warnings.append(
                    warning(
                        "SNAP_NOT_APPLICABLE",
                        "No SNAP schedule is known for "
                        f"{', '.join(unknown_states[:MAX_EXAMPLES])}, and the data has no snap_day "
                        "column, so the SNAP feature is left out of training. SNAP scenarios will "
                        "not be offered for a model trained on this dataset.",
                        count=len(unknown_states),
                        stage="readiness",
                    )
                )
    if "is_closed" not in frame.columns:
        if options.closure in ("omit", "source"):
            context["is_closed"] = "not used (no opening status; no closure calendar applied)"
        else:
            closed = (frame["date"].dt.month == CLOSED_MONTH_DAY[0]) & (
                frame["date"].dt.day == CLOSED_MONTH_DAY[1]
            )
            frame["is_closed"] = closed.astype("float64")
            context["is_closed"] = "derived from the platform closure calendar (25 December)"
            open_on_closure = closed & (frame["sales"] > 0)
            if open_on_closure.any():
                warnings.append(
                    warning(
                        "SALES_ON_CLOSED_DAY",
                        "Sales were recorded on 25 December, which the platform treats as closed. "
                        "Add an is_closed column if these stores trade that day.",
                        count=int(open_on_closure.sum()),
                        examples=frame[open_on_closure]
                        .head(MAX_EXAMPLES)
                        .assign(date=lambda f: f["date"].dt.date.astype(str))[["store_id", "date", "sales"]]
                        .to_dict("records"),
                    )
                )

    frame = frame.drop(columns=["_row"]).sort_values(key).reset_index(drop=True)

    # -- continuity --------------------------------------------------------
    gap_stores: Dict[str, Dict[str, Any]] = {}
    for store, group in frame.groupby("store_id", sort=True):
        days = group["date"]
        expected = pd.date_range(days.min(), days.max(), freq="D")
        missing_days = expected.difference(pd.DatetimeIndex(days))
        if len(missing_days):
            gap_stores[store] = {
                "store_id": store,
                "missing": int(len(missing_days)),
                "first_missing": missing_days[0].date().isoformat(),
                "last_missing": missing_days[-1].date().isoformat(),
            }
    excluded_stores: List[str] = []
    if gap_stores:
        total_missing = sum(item["missing"] for item in gap_stores.values())
        if options.missing_dates == "exclude_stores":
            excluded_stores = sorted(gap_stores)
            frame = frame[~frame["store_id"].isin(excluded_stores)].reset_index(drop=True)
            warnings.append(
                warning(
                    "STORES_EXCLUDED",
                    f"{len(excluded_stores)} store(s) with days missing inside their history were "
                    f"excluded under the chosen policy ({total_missing:,} missing store-days). "
                    "A model trained on this version cannot forecast them.",
                    count=len(excluded_stores),
                    examples=list(gap_stores.values())[:MAX_EXAMPLES],
                    stage="readiness",
                    requires_ack=True,
                )
            )
        else:
            errors.append(
                Issue(
                    "MISSING_DATES",
                    f"{len(gap_stores)} store(s) have days missing inside their history "
                    f"({total_missing:,} store-days). Lag features need every day.",
                    len(gap_stores),
                    list(gap_stores.values())[:MAX_EXAMPLES],
                    stage="readiness",
                    action=(
                        "Add the missing days (sales 0 where a store genuinely sold nothing), or "
                        "choose the 'exclude stores with gaps' policy."
                    ),
                )
            )
    if frame.empty:
        errors.append(Issue("EMPTY_DATASET", "No store remains after the chosen policies.", stage="readiness"))
        return finish(None, counts, rows_checked=True)

    # -- history -----------------------------------------------------------
    stores_report: List[Dict[str, Any]] = []
    short_issue = Issue(
        "INSUFFICIENT_HISTORY",
        f"Each store needs at least {policy.min_store_days} days "
        f"({policy.warmup_days} days of feature warm-up plus "
        f"{policy.min_train_days_after_warmup} training days) before the validation window.",
        stage="readiness",
        action="Add older history for these stores, or remove them.",
    )
    split = split_dates(frame["date"].tolist(), policy)
    test_start = pd.Timestamp(split["test"][0]) if split.get("valid") else None
    train_end = pd.Timestamp(split["train"][1]) if split.get("valid") else None
    not_in_test: List[str] = []
    summary = frame.groupby("store_id", sort=True).agg(
        days=("date", "size"),
        first=("date", "min"),
        last=("date", "max"),
        zero=("sales", lambda values: int((values == 0).sum())),
        total=("sales", "sum"),
    )
    if train_end is not None:
        before = frame[frame["date"] <= train_end].groupby("store_id").size()
    for store, row in summary.iterrows():
        gap = gap_stores.get(store)
        entry = {
            "store_id": store,
            "days": int(row["days"]),
            "first_date": row["first"].date().isoformat(),
            "last_date": row["last"].date().isoformat(),
            "missing_dates": gap["missing"] if gap else 0,
            "zero_sales_days": int(row["zero"]),
            "total_sales": round(float(row["total"]), 2),
            "sufficient": gap is None,
            "reason": f"{gap['missing']} missing day(s) from {gap['first_missing']}" if gap else None,
        }
        if require_history:
            if row["days"] < policy.min_store_days:
                short_issue.count += 1
                short_issue.examples.append({"store_id": store, "days": int(row["days"])})
                entry["sufficient"] = False
                entry["reason"] = f"only {int(row['days'])} day(s); needs {policy.min_store_days}"
            elif train_end is not None:
                train_days = int(before.get(store, 0))
                if train_days < policy.min_store_days:
                    short_issue.count += 1
                    short_issue.examples.append({"store_id": store, "days_before_validation": train_days})
                    entry["sufficient"] = False
                    entry["reason"] = (
                        f"only {train_days} day(s) before the validation window; needs {policy.min_store_days}"
                    )
                if row["last"] < test_start:
                    not_in_test.append(store)
        stores_report.append(entry)
    if short_issue.count:
        errors.append(short_issue)
    if not_in_test:
        warnings.append(
            warning(
                "STORE_NOT_IN_TEST_WINDOW",
                f"{len(not_in_test)} store(s) have no days in the test window, so their accuracy is not measured.",
                count=len(not_in_test),
                examples=[{"store_id": store} for store in not_in_test[:MAX_EXAMPLES]],
                stage="readiness",
            )
        )

    total_dates = int(frame["date"].nunique())
    if require_history and (total_dates < policy.min_total_dates or not split.get("valid")):
        errors.append(
            Issue(
                "INSUFFICIENT_EVALUATION_WINDOW",
                f"{total_dates} distinct dates; at least {policy.min_total_dates} are needed for "
                f"chronological training, validation ({policy.min_validation_days}+ days) and an "
                f"untouched test window ({policy.min_test_days}+ days).",
                stage="readiness",
            )
        )

    preview = (
        frame.head(PREVIEW_ROWS)
        .assign(date=lambda f: f["date"].dt.date.astype(str))
        .to_dict("records")
    )
    for column in CONTEXT_COLUMNS:
        if column in frame.columns:
            frame[column] = frame[column].astype("int64")
    extra = {
        **counts,
        "rows_normalized": rows_received,
        "rows_accepted": int(len(frame)),
        "exact_duplicates_removed": exact_removed,
        "conflicting_duplicates": conflict_count,
        "date_min": frame["date"].min().date().isoformat(),
        "date_max": frame["date"].max().date().isoformat(),
        "total_dates": total_dates,
        "store_count": int(frame["store_id"].nunique()),
        "stores_excluded": excluded_stores,
        "stores": stores_report,
        "context_features": context,
        "feature_columns": [column for column in CONTEXT_COLUMNS if column in frame.columns],
        "split": split,
        "preview": preview,
    }
    result = finish(frame, extra, rows_checked=True)
    result.report.update(counts)
    return result


# ---------------------------------------------------------------------------
# Canonical form and hashing
# ---------------------------------------------------------------------------


def canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Column order, row order and dtypes fixed, so equal data hashes equal."""
    columns = list(REQUIRED_COLUMNS) + [column for column in CONTEXT_COLUMNS if column in frame.columns]
    out = frame[columns].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["store_id"] = out["store_id"].astype(str)
    out["sales"] = out["sales"].astype("float64")
    for column in CONTEXT_COLUMNS:
        if column in out.columns:
            out[column] = out[column].astype("int64")
    return out.sort_values(["store_id", "date"]).reset_index(drop=True)


def content_hash(frame: pd.DataFrame) -> str:
    """
    SHA-256 of the dataset's content, independent of file format.

    Hashes a fixed text rendering rather than Parquet bytes: two Parquet
    writers can encode identical data differently, and the hash has to mean
    "same records", not "same encoder".
    """
    canonical = canonical_frame(frame).copy()
    canonical["date"] = canonical["date"].dt.strftime("%Y-%m-%d")
    text = canonical.to_csv(index=False, float_format="%.6f", lineterminator="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def to_parquet_bytes(frame: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    canonical_frame(frame).to_parquet(buffer, index=False)
    return buffer.getvalue()


def from_parquet_bytes(content: bytes) -> pd.DataFrame:
    return canonical_frame(pd.read_parquet(io.BytesIO(content)))


# ---------------------------------------------------------------------------
# Combination
# ---------------------------------------------------------------------------


@dataclass
class CombineResult:
    frame: Optional[pd.DataFrame]
    report: Dict[str, Any]


def combine(
    base: pd.DataFrame,
    base_meta: Dict[str, Any],
    new: pd.DataFrame,
    new_meta: Dict[str, Any],
    *,
    conflict_policy: str,
) -> CombineResult:
    """
    Append a validated upload to an approved dataset.

    Refuses anything that would make the result mean two things at once:
    different target, currency, frequency, schema, source namespace or source
    type (real and synthetic are never blended), or context features one side
    has and the other lacks. Overlapping store-days that agree are
    de-duplicated; ones that disagree are refused unless ``prefer_new`` -- the
    documented correction policy -- was chosen. Neither input is modified.
    """
    errors: List[Issue] = []
    warnings: List[Issue] = []
    for key in ("schema_version", "target", "frequency", "currency", "source_namespace", "target_type"):
        if key in base_meta or key in new_meta:
            if base_meta.get(key) != new_meta.get(key):
                errors.append(
                    Issue(
                        "APPEND_INCOMPATIBLE",
                        f"{key} differs: base dataset has {base_meta.get(key)!r}, upload has {new_meta.get(key)!r}.",
                        stage="append",
                        column=key,
                    )
                )
    base_sources = set(base_meta.get("source_types") or [])
    new_sources = set(new_meta.get("source_types") or [])
    if base_sources != new_sources:
        errors.append(
            Issue(
                "MIXED_SOURCE_TYPES",
                f"The base dataset is {', '.join(sorted(base_sources)) or 'unknown'} data and the upload is "
                f"{', '.join(sorted(new_sources)) or 'unknown'}. Real and synthetic records are not combined.",
                stage="append",
                action="Train the upload as an independent dataset instead.",
            )
        )
    if conflict_policy not in CONFLICT_POLICIES:
        errors.append(
            Issue("INVALID_POLICY", f"Conflict policy must be one of {CONFLICT_POLICIES}.", stage="append")
        )

    base_c = canonical_frame(base)
    new_c = canonical_frame(new)
    if set(base_c.columns) != set(new_c.columns):
        errors.append(
            Issue(
                "APPEND_INCOMPATIBLE",
                "The context features differ: base has "
                f"{sorted(set(base_c.columns) - set(REQUIRED_COLUMNS))}, upload has "
                f"{sorted(set(new_c.columns) - set(REQUIRED_COLUMNS))}.",
                stage="append",
            )
        )
    base_stores = set(base_c["store_id"])
    new_stores = set(new_c["store_id"])
    shared = base_stores & new_stores
    if not shared:
        errors.append(
            Issue(
                "APPEND_INCOMPATIBLE",
                "The upload shares no store with the base dataset, so it is a different "
                "store domain, not more history.",
                stage="append",
                action="Train it as an independent dataset.",
            )
        )
    added_stores = sorted(new_stores - base_stores)
    if added_stores and shared:
        warnings.append(
            warning(
                "NEW_STORES",
                f"Stores not in the base dataset: {', '.join(added_stores[:MAX_EXAMPLES])}.",
                count=len(added_stores),
                stage="append",
            )
        )
    report: Dict[str, Any] = {
        "base_rows": int(len(base_c)),
        "upload_rows": int(len(new_c)),
        "conflict_policy": conflict_policy,
        "shared_stores": sorted(shared)[:50],
        "new_stores": added_stores[:50],
    }
    if errors:
        report["errors"] = [issue.as_dict() for issue in errors]
        report["warnings"] = [issue.as_dict() for issue in warnings]
        return CombineResult(None, report)

    key = ["store_id", "date"]
    merged = base_c.merge(new_c, on=key, how="inner", suffixes=("_base", "_new"))
    value_columns = [column for column in base_c.columns if column not in key]
    agrees = pd.Series(True, index=merged.index)
    for column in value_columns:
        if column == "sales":
            agrees &= (merged["sales_base"] - merged["sales_new"]).abs() <= 1e-6
        else:
            agrees &= merged[f"{column}_base"] == merged[f"{column}_new"]
    duplicates = int(agrees.sum())
    conflicting = merged[~agrees]
    report["overlapping_store_days"] = int(len(merged))
    report["exact_duplicates_removed"] = duplicates
    report["conflicts"] = int(len(conflicting))
    if len(conflicting) and conflict_policy == "reject":
        errors.append(
            Issue(
                "CONFLICTING_RECORDS",
                "The upload changes values the base dataset already holds.",
                int(len(conflicting)),
                conflicting.head(MAX_EXAMPLES)
                .assign(date=lambda f: f["date"].dt.date.astype(str))[
                    ["store_id", "date", "sales_base", "sales_new"]
                ]
                .to_dict("records"),
                stage="append",
                action="Choose the 'prefer_new' correction policy to replace them, or remove the overlap.",
            )
        )
        report["errors"] = [issue.as_dict() for issue in errors]
        report["warnings"] = [issue.as_dict() for issue in warnings]
        return CombineResult(None, report)
    if len(conflicting):
        warnings.append(
            warning(
                "CORRECTIONS_APPLIED",
                "Overlapping store-days with different values were replaced by the upload "
                "(prefer_new correction policy).",
                count=int(len(conflicting)),
                stage="append",
            )
        )
    keep_base = base_c.merge(new_c[key], on=key, how="left", indicator=True)
    keep_base = keep_base[keep_base["_merge"] == "left_only"].drop(columns=["_merge"])
    combined = canonical_frame(pd.concat([keep_base, new_c], ignore_index=True))
    report["corrections_applied"] = int(len(conflicting)) if conflict_policy == "prefer_new" else 0
    report["rows"] = int(len(combined))
    report["warnings"] = [issue.as_dict() for issue in warnings]
    return CombineResult(combined, report)


def frame_to_raw(frame: pd.DataFrame) -> pd.DataFrame:
    """A clean frame in upload form, so a combination can be re-validated."""
    out = canonical_frame(frame).copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    return out.astype({"sales": "float64"})


def stores_of(frame: pd.DataFrame) -> List[str]:
    return sorted(str(value) for value in frame["store_id"].unique())


def iter_issue_codes(report: Dict[str, Any], kind: str = "errors") -> Iterable[str]:
    return (issue["code"] for issue in report.get(kind) or [])
