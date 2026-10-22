"""
The store-day training contract: schema, validation, hashing, and combination.

A training upload is **store-day sales**: one row per store per calendar day,
with ``sales`` the day's revenue in the declared currency. That is the grain the
model is trained and served at -- the training DAG reduces M5's product rows to
store-day totals before fitting -- so it is the grain an upload must arrive at.
Product-level rows are refused with an explanation rather than summed here:
whether two product rows for one store-day are additive (or a correction of
each other) is a business question this code cannot answer.

This is not the history sent with a single forecast request (a short series
the API never stores) and not the actual-sales feed monitoring scores against
(the ``actuals`` table). Those are separate workflows with their own rules.

Nothing in here touches storage or a database. Every function takes a frame
and a policy and returns a result, which is what lets the API and the DAG run
the identical check and the test suite exercise it without services.
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
TARGET_DESCRIPTION = "Total daily revenue of one store, in the dataset currency."
FREQUENCY = "D"

REQUIRED_COLUMNS: Tuple[str, ...] = ("date", "store_id", "sales")

#: Optional context columns and the model feature each one becomes. Nothing
#: outside this mapping is accepted: an unmapped column would either be silently
#: dropped or become a feature serving cannot reproduce.
CONTEXT_COLUMNS: Dict[str, str] = {
    "snap_day": "1 on a day SNAP benefits are issued in the store's state, else 0.",
    "is_closed": "1 on a day the store was closed, else 0.",
}

#: Columns that mean the file is at product grain, not store-day grain.
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

STORE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
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
    """

    max_bytes: int = 50 * 1024 * 1024
    max_rows: int = 2_000_000
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
        allowed = _env_list("TRAINING_ALLOWED_STORES")
        currencies = _env_list("TRAINING_ALLOWED_CURRENCIES") or ["USD"]
        negative = os.getenv("TRAINING_NEGATIVE_SALES_POLICY", "reject").strip().lower()
        return cls(
            max_bytes=_env_int("TRAINING_UPLOAD_MAX_BYTES", 50 * 1024 * 1024),
            max_rows=_env_int("TRAINING_UPLOAD_MAX_ROWS", 2_000_000),
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


def contract_description(policy: ValidationPolicy) -> Dict[str, Any]:
    """The accepted schema, as shown on the upload card."""
    return {
        "schema_version": SCHEMA_VERSION,
        "grain": "One row per store per calendar day.",
        "target": TARGET,
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
                "If omitted, derived from the state SNAP schedule for stores whose "
                "identifier starts with CA_, TX_ or WI_; for any other store the "
                "feature is left out of training."
            ),
            "is_closed": "If omitted, derived from the platform closure calendar (25 December).",
        },
        "rejected": (
            "Product-level rows (product_id, item_id, sku, quantity_sold, ...). "
            "Aggregate them to store-day revenue first."
        ),
        "file_types": ["csv", "parquet"],
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
    Parse an upload into a frame of raw values.

    CSV cells are read as text, so parsing (dates, numbers) happens in
    ``validate`` where each failure can be reported with its row number.
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
# Validation
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    """One problem, with enough detail to fix it."""

    code: str
    message: str
    count: int = 0
    examples: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "count": self.count,
            "examples": self.examples[:MAX_EXAMPLES],
        }


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
    examples = []
    for index in raw.index[mask][:MAX_EXAMPLES]:
        example: Dict[str, Any] = {"row": int(index) + 2}
        for column in columns:
            if column in raw.columns:
                value = raw.at[index, column]
                example[column] = None if pd.isna(value) else str(value)
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
    parsed = pd.to_datetime(text.where(well_formed), format="%Y-%m-%d", errors="coerce")
    return parsed


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

    Splitting by date rather than by row position is what keeps one day's
    stores together: a position split can put CA_1's 1 March in training and
    TX_1's 1 March in test, which leaks the day into evaluation.
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


def validate(
    raw: pd.DataFrame,
    policy: ValidationPolicy,
    *,
    currency: str,
    source_type: str,
    require_history: bool = True,
) -> ValidationResult:
    """
    Check an upload against the contract and return a report.

    ``require_history=False`` is for the upload half of an append: rows, grain
    and continuity are checked, but history length and the date split are
    judged on the combined dataset, where they apply.

    Every rule that fails adds an issue; none of them silently drops or repairs
    a record. The one transformation applied is removing *exact* duplicate
    store-days (identical in every column), which is reported with a count --
    keeping one of two identical rows changes no number the model sees.
    """
    errors: List[Issue] = []
    warnings: List[Issue] = []
    rows_received = int(len(raw))

    def finish(frame: Optional[pd.DataFrame], extra: Optional[Dict[str, Any]] = None) -> ValidationResult:
        report: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "valid" if frame is not None and not errors else "invalid",
            "target": TARGET,
            "frequency": FREQUENCY,
            "currency": currency,
            "source_type": source_type,
            "rows_received": rows_received,
            "errors": [issue.as_dict() for issue in errors],
            "warnings": [issue.as_dict() for issue in warnings],
            "policy": policy.describe(),
        }
        report.update(extra or {})
        return ValidationResult(report=report, frame=frame if not errors else None)

    # -- declared metadata -------------------------------------------------
    if not CURRENCY_PATTERN.match(currency or ""):
        errors.append(Issue("invalid_currency", "Currency must be a 3-letter ISO code, e.g. USD."))
    elif currency not in policy.allowed_currencies:
        errors.append(
            Issue(
                "incompatible_currency",
                f"Currency {currency} is not one this platform's models are trained in "
                f"({', '.join(policy.allowed_currencies)}). Revenue in another currency "
                "would be a different target.",
            )
        )
    if source_type not in SOURCE_TYPES:
        errors.append(Issue("invalid_source_type", "Source type must be 'real' or 'synthetic'."))

    # -- shape -------------------------------------------------------------
    if rows_received == 0:
        errors.append(Issue("empty_file", "The file has no data rows."))
        return finish(None)
    if rows_received > policy.max_rows:
        errors.append(
            Issue("too_many_rows", f"{rows_received:,} rows exceeds the limit of {policy.max_rows:,}.")
        )
        return finish(None)

    columns = list(raw.columns)
    duplicated_columns = sorted({column for column in columns if columns.count(column) > 1})
    if duplicated_columns:
        errors.append(
            Issue("duplicate_columns", f"Columns appear more than once: {', '.join(duplicated_columns)}.")
        )
        return finish(None)

    product_columns = [column for column in columns if column in PRODUCT_GRAIN_COLUMNS]
    if product_columns:
        errors.append(
            Issue(
                "product_grain",
                "This looks like product-level data (found "
                f"{', '.join(product_columns)}). Training uploads are store-day totals: "
                "one row per store per day with that day's revenue in 'sales'.",
            )
        )
    missing = [column for column in REQUIRED_COLUMNS if column not in columns]
    if missing:
        errors.append(
            Issue("missing_columns", f"Required columns are missing: {', '.join(missing)}.")
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
                "unmapped_columns",
                f"Columns with no mapping to a model feature: {', '.join(unknown)}. "
                f"Remove them; the only optional columns are {', '.join(CONTEXT_COLUMNS)}.",
            )
        )
    if missing or product_columns:
        return finish(None)

    # -- values ------------------------------------------------------------
    parsed = pd.DataFrame(index=raw.index)
    parsed["date"] = _parse_dates(raw["date"])
    bad_date = parsed["date"].isna()
    if bad_date.any():
        errors.append(
            Issue(
                "invalid_date",
                "Dates must be calendar days written YYYY-MM-DD (no time of day).",
                int(bad_date.sum()),
                _row_examples(raw, bad_date, ["date"]),
            )
        )

    store_text = raw["store_id"].astype("string").str.strip()
    parsed["store_id"] = store_text
    bad_store = store_text.isna() | ~store_text.fillna("").str.match(STORE_ID_PATTERN.pattern)
    if bad_store.any():
        errors.append(
            Issue(
                "invalid_store_id",
                "store_id must be letters, digits, '_' or '-' (up to 64 characters) and not empty.",
                int(bad_store.sum()),
                _row_examples(raw, bad_store, ["store_id"]),
            )
        )
    if policy.allowed_stores is not None:
        outside = ~bad_store & ~store_text.isin(policy.allowed_stores)
        if outside.any():
            errors.append(
                Issue(
                    "store_out_of_scope",
                    "These stores are outside the store scope this deployment may train on.",
                    int(outside.sum()),
                    _row_examples(raw, outside, ["store_id"]),
                )
            )

    sales = pd.to_numeric(raw["sales"], errors="coerce")
    blank_sales = raw["sales"].isna() | (raw["sales"].astype("string").str.strip() == "")
    if blank_sales.any():
        errors.append(
            Issue(
                "null_target",
                "sales is empty. A day with no sales is 0, not blank.",
                int(blank_sales.sum()),
                _row_examples(raw, blank_sales, ["store_id", "date", "sales"]),
            )
        )
    not_numeric = ~blank_sales & (sales.isna() | ~sales.map(lambda v: math.isfinite(v) if pd.notna(v) else False))
    if not_numeric.any():
        errors.append(
            Issue(
                "invalid_sales",
                "sales must be a finite number.",
                int(not_numeric.sum()),
                _row_examples(raw, not_numeric, ["store_id", "date", "sales"]),
            )
        )
    parsed["sales"] = sales.astype("float64")
    negative = parsed["sales"] < 0
    if negative.any():
        issue = Issue(
            "negative_sales",
            "Negative daily revenue."
            + (
                " Allowed by the returns policy (net of returns)."
                if policy.negative_sales == "allow"
                else " The returns policy rejects it; correct the rows or ask an "
                "administrator to allow net-of-returns revenue."
            ),
            int(negative.sum()),
            _row_examples(raw, negative, ["store_id", "date", "sales"]),
        )
        (warnings if policy.negative_sales == "allow" else errors).append(issue)

    context: Dict[str, str] = {}
    for column in CONTEXT_COLUMNS:
        if column not in columns:
            continue
        flags = _parse_flags(raw[column])
        bad_flag = flags.isna()
        if bad_flag.any():
            errors.append(
                Issue(
                    f"invalid_{column}",
                    f"{column} must be 0 or 1 on every row; a partly filled column cannot be used.",
                    int(bad_flag.sum()),
                    _row_examples(raw, bad_flag, ["store_id", "date", column]),
                )
            )
        parsed[column] = flags
        context[column] = "provided"

    row_ok = ~(bad_date | bad_store | blank_sales | not_numeric)
    if errors:
        # Grain checks need parseable keys; report what is known so far, plus
        # duplicate keys among the rows that did parse.
        valid_keys = parsed[row_ok]
        dup = valid_keys.duplicated(["store_id", "date"], keep=False)
        if dup.any():
            warnings.append(
                Issue(
                    "duplicates_pending",
                    "Some store-days appear more than once; they are checked once the errors above are fixed.",
                    int(dup.sum()),
                )
            )
        return finish(None, {"rows_valid": int(row_ok.sum()), "rows_invalid": int((~row_ok).sum())})

    # -- grain: one row per store-day -------------------------------------
    frame = parsed.copy()
    frame["_row"] = frame.index + 2
    key = ["store_id", "date"]
    value_columns = ["sales"] + [column for column in CONTEXT_COLUMNS if column in frame.columns]
    exact = frame.duplicated(key + value_columns, keep="first")
    exact_removed = int(exact.sum())
    frame = frame[~exact]
    conflicts = frame.duplicated(key, keep=False)
    if conflicts.any():
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
                "conflicting_duplicates",
                "The same store-day appears more than once with different values. "
                "Keep one row per store per day.",
                int(frame[conflicts].drop_duplicates(key).shape[0]),
                examples,
            )
        )
    if exact_removed:
        warnings.append(
            Issue(
                "exact_duplicates_removed",
                "Identical repeated rows were collapsed to one (first occurrence kept).",
                exact_removed,
            )
        )

    # -- context derivation -----------------------------------------------
    if "snap_day" not in frame.columns:
        derived = [snap_flag(store, day) for store, day in zip(frame["store_id"], frame["date"])]
        if all(value is not None for value in derived):
            frame["snap_day"] = [float(value) for value in derived]
            context["snap_day"] = "derived from the state SNAP schedule"
        else:
            unknown_states = sorted(
                {store for store, value in zip(frame["store_id"], derived) if value is None}
            )
            context["snap_day"] = "unavailable"
            warnings.append(
                Issue(
                    "snap_unavailable",
                    "No SNAP schedule is known for "
                    f"{', '.join(unknown_states[:MAX_EXAMPLES])}, and the file has no snap_day "
                    "column, so the SNAP feature is left out of training. SNAP scenarios will "
                    "not be offered for a model trained on this dataset.",
                    len(unknown_states),
                )
            )
    if "is_closed" not in frame.columns:
        closed = (frame["date"].dt.month == CLOSED_MONTH_DAY[0]) & (
            frame["date"].dt.day == CLOSED_MONTH_DAY[1]
        )
        frame["is_closed"] = closed.astype("float64")
        context["is_closed"] = "derived from the platform closure calendar (25 December)"
        open_on_closure = closed & (frame["sales"] > 0)
        if open_on_closure.any():
            warnings.append(
                Issue(
                    "sales_on_closure_day",
                    "Sales were recorded on 25 December, which the platform treats as closed. "
                    "Add an is_closed column if these stores trade that day.",
                    int(open_on_closure.sum()),
                    frame[open_on_closure]
                    .head(MAX_EXAMPLES)
                    .assign(date=lambda f: f["date"].dt.date.astype(str))[["store_id", "date", "sales"]]
                    .to_dict("records"),
                )
            )

    frame = frame.drop(columns=["_row"]).sort_values(key).reset_index(drop=True)

    # -- continuity and history -------------------------------------------
    stores_report: List[Dict[str, Any]] = []
    gap_issue = Issue(
        "missing_dates",
        "Days are missing inside a store's history. Lag features need every day: add the "
        "missing days, with sales 0 where the store genuinely sold nothing.",
    )
    short_issue = Issue(
        "insufficient_history",
        f"Each store needs at least {policy.min_store_days} days "
        f"({policy.warmup_days} days of feature warm-up plus "
        f"{policy.min_train_days_after_warmup} training days).",
    )
    split = split_dates(frame["date"].tolist(), policy)
    for store, group in frame.groupby("store_id", sort=True):
        days = group["date"]
        expected = pd.date_range(days.min(), days.max(), freq="D")
        missing_days = expected.difference(pd.DatetimeIndex(days))
        entry = {
            "store_id": store,
            "days": int(len(group)),
            "first_date": days.min().date().isoformat(),
            "last_date": days.max().date().isoformat(),
            "missing_dates": int(len(missing_days)),
            "zero_sales_days": int((group["sales"] == 0).sum()),
            "total_sales": round(float(group["sales"].sum()), 2),
            "sufficient": True,
            "reason": None,
        }
        if len(missing_days):
            gap_issue.count += len(missing_days)
            gap_issue.examples.append(
                {
                    "store_id": store,
                    "missing": int(len(missing_days)),
                    "first_missing": missing_days[0].date().isoformat(),
                }
            )
            entry["sufficient"] = False
            entry["reason"] = f"{len(missing_days)} missing day(s)"
        if not require_history:
            pass
        elif len(group) < policy.min_store_days:
            short_issue.count += 1
            short_issue.examples.append({"store_id": store, "days": int(len(group))})
            entry["sufficient"] = False
            entry["reason"] = f"only {len(group)} day(s); needs {policy.min_store_days}"
        elif split.get("valid"):
            train_end = pd.Timestamp(split["train"][1])
            train_days = int((days <= train_end).sum())
            if train_days < policy.min_store_days:
                short_issue.count += 1
                short_issue.examples.append(
                    {"store_id": store, "days_before_validation": train_days}
                )
                entry["sufficient"] = False
                entry["reason"] = (
                    f"only {train_days} day(s) before the validation window; needs {policy.min_store_days}"
                )
            if days.max() < pd.Timestamp(split["test"][0]):
                warnings.append(
                    Issue(
                        "store_not_in_test",
                        f"{store} has no days in the test window, so its accuracy is not measured.",
                        1,
                    )
                )
        stores_report.append(entry)
    if gap_issue.count:
        errors.append(gap_issue)
    if short_issue.count:
        errors.append(short_issue)

    total_dates = int(frame["date"].nunique())
    if require_history and (total_dates < policy.min_total_dates or not split.get("valid")):
        errors.append(
            Issue(
                "insufficient_dates",
                f"{total_dates} distinct dates; at least {policy.min_total_dates} are needed for "
                f"chronological training, validation ({policy.min_validation_days}+ days) and an "
                f"untouched test window ({policy.min_test_days}+ days).",
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
        "rows_valid": int(len(frame)),
        "rows_invalid": 0,
        "exact_duplicates_removed": exact_removed,
        "conflicting_duplicates": next(
            (issue.count for issue in errors if issue.code == "conflicting_duplicates"), 0
        ),
        "date_min": frame["date"].min().date().isoformat(),
        "date_max": frame["date"].max().date().isoformat(),
        "total_dates": total_dates,
        "store_count": int(frame["store_id"].nunique()),
        "stores": stores_report,
        "context_features": context,
        "feature_columns": [column for column in CONTEXT_COLUMNS if column in frame.columns],
        "split": split,
        "preview": preview,
    }
    return finish(frame, extra)


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
    different target, currency, frequency, schema, or source type (real and
    synthetic are never blended), or context features one side has and the
    other lacks. Overlapping store-days that agree are de-duplicated; ones that
    disagree are refused unless ``prefer_new`` -- the documented correction
    policy -- was chosen, in which case the upload's value wins and the count
    is recorded. Neither input is modified.
    """
    errors: List[Issue] = []
    warnings: List[Issue] = []
    for key in ("schema_version", "target", "frequency", "currency"):
        if base_meta.get(key) != new_meta.get(key):
            errors.append(
                Issue(
                    f"incompatible_{key}",
                    f"{key} differs: base dataset has {base_meta.get(key)!r}, upload has {new_meta.get(key)!r}.",
                )
            )
    base_sources = set(base_meta.get("source_types") or [])
    new_sources = set(new_meta.get("source_types") or [])
    if base_sources != new_sources:
        errors.append(
            Issue(
                "mixed_sources",
                f"The base dataset is {', '.join(sorted(base_sources)) or 'unknown'} data and the upload is "
                f"{', '.join(sorted(new_sources)) or 'unknown'}. Real and synthetic records are not "
                "combined; train the upload as an independent dataset instead.",
            )
        )
    if conflict_policy not in CONFLICT_POLICIES:
        errors.append(Issue("invalid_conflict_policy", f"Conflict policy must be one of {CONFLICT_POLICIES}."))

    base_c = canonical_frame(base)
    new_c = canonical_frame(new)
    if set(base_c.columns) != set(new_c.columns):
        errors.append(
            Issue(
                "incompatible_features",
                "The context features differ: base has "
                f"{sorted(set(base_c.columns) - set(REQUIRED_COLUMNS))}, upload has "
                f"{sorted(set(new_c.columns) - set(REQUIRED_COLUMNS))}.",
            )
        )
    base_stores = set(base_c["store_id"])
    new_stores = set(new_c["store_id"])
    shared = base_stores & new_stores
    if not shared:
        errors.append(
            Issue(
                "no_common_stores",
                "The upload shares no store with the base dataset, so it is a different "
                "store domain, not more history. Train it as an independent dataset.",
            )
        )
    added_stores = sorted(new_stores - base_stores)
    if added_stores and shared:
        warnings.append(
            Issue(
                "new_stores",
                f"Stores not in the base dataset: {', '.join(added_stores[:MAX_EXAMPLES])}.",
                len(added_stores),
            )
        )
    report: Dict[str, Any] = {
        "base_rows": int(len(base_c)),
        "upload_rows": int(len(new_c)),
        "conflict_policy": conflict_policy,
        "shared_stores": sorted(shared),
        "new_stores": added_stores,
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
                "conflicting_overlap",
                "The upload changes values the base dataset already holds. Choose the "
                "'prefer_new' correction policy to replace them, or remove the overlap.",
                int(len(conflicting)),
                conflicting.head(MAX_EXAMPLES)
                .assign(date=lambda f: f["date"].dt.date.astype(str))[
                    ["store_id", "date", "sales_base", "sales_new"]
                ]
                .to_dict("records"),
            )
        )
        report["errors"] = [issue.as_dict() for issue in errors]
        report["warnings"] = [issue.as_dict() for issue in warnings]
        return CombineResult(None, report)
    if len(conflicting):
        warnings.append(
            Issue(
                "corrections_applied",
                "Overlapping store-days with different values were replaced by the upload "
                "(prefer_new correction policy).",
                int(len(conflicting)),
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
