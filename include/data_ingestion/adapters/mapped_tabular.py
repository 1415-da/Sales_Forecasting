"""
Any single CSV/Parquet store-day table, through an explicit confirmed mapping.

Nothing is assumed from a column's name: suggestions are offered (a column
called ``branch_id`` is probably a store), but the user maps each field and
confirms what the target *means*. A column called ``sales`` may be units;
only a confirmed revenue target is accepted for this revenue configuration.

Supported: one row per store per day (or one store, confirmed explicitly),
daily frequency, revenue target. Not supported: product- or transaction-level
rows (there is no aggregation policy here), quantity targets, other
frequencies -- each is refused with what would be needed instead.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import pandas as pd

from data_ingestion.adapters.base import (
    MISSING_DATES_CONFIRMATION,
    Adapter,
    Confirmation,
    Detection,
    Normalized,
    currency_confirmation,
)
from data_ingestion.package_inspector import DataFile, Package
from training import contract
from training.contract import FeatureOptions, Issue

#: Date formats a user may choose for non-ISO dates.
DATE_FORMATS = {
    "%Y-%m-%d": "2025-01-31",
    "%d/%m/%Y": "31/01/2025",
    "%m/%d/%Y": "01/31/2025",
    "%d.%m.%Y": "31.01.2025",
    "%Y/%m/%d": "2025/01/31",
    "%d-%m-%Y": "31-01-2025",
    "%m-%d-%Y": "01-31-2025",
    "%Y%m%d": "20250131",
}

SUGGEST = {
    "date": re.compile(r"(^|_)(date|day|ds|transaction_date|sale_date|order_date)($|_)"),
    "store_id": re.compile(r"(store|branch|shop|location|site|outlet)"),
    "sales": re.compile(r"(sales|revenue|turnover|amount|takings)"),
    "is_closed": re.compile(r"(closed|is_closed)"),
    "open": re.compile(r"^(open|is_open)$"),
}

CONFIRM_TARGET = Confirmation(
    key="target_type",
    label="What does the target column measure?",
    options=[
        {"value": "revenue", "label": "Revenue (money taken per store per day)"},
        {"value": "quantity", "label": "Quantity (units sold)"},
    ],
    help="This training configuration forecasts revenue. Units need a separate demand configuration.",
)
CONFIRM_FREQUENCY = Confirmation(
    key="frequency",
    label="How often is there a row per store?",
    options=[{"value": "daily", "label": "Daily"}, {"value": "weekly", "label": "Weekly"}, {"value": "monthly", "label": "Monthly"}],
)
CONFIRM_GRAIN = Confirmation(
    key="source_grain",
    label="What does one row represent?",
    options=[
        {"value": "store_day", "label": "One store on one day (already totalled)"},
        {"value": "transaction", "label": "One transaction or receipt"},
        {"value": "product_day", "label": "One product in one store on one day"},
    ],
)


def _suggest(columns: List[str]) -> Dict[str, Optional[str]]:
    lowered = {column: column.strip().casefold() for column in columns}
    suggestions: Dict[str, Optional[str]] = {}
    for target, pattern in SUGGEST.items():
        hits = [column for column, low in lowered.items() if pattern.search(low)]
        suggestions[target] = hits[0] if len(hits) == 1 else None
    return suggestions


def _date_samples(data: DataFile, column: str) -> List[str]:
    sample = Adapter.read_text_table(data, [column], 500)[column].dropna().astype(str).str.strip()
    return list(sample.unique()[:200])


def date_format_status(samples: List[str]) -> Dict[str, Any]:
    """Which formats parse every sample; ambiguous when more than one does."""
    if samples and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in samples):
        return {"iso": True, "candidates": ["%Y-%m-%d"], "ambiguous": False}
    candidates = []
    for fmt in DATE_FORMATS:
        parsed = pd.to_datetime(pd.Series(samples), format=fmt, errors="coerce")
        if len(samples) and parsed.notna().all():
            candidates.append(fmt)
    return {"iso": False, "candidates": candidates, "ambiguous": len(candidates) != 1}


class MappedTabularAdapter(Adapter):
    id = "mapped_tabular"
    version = "1"
    label = "Other store-day table (manual column mapping)"

    def detect(self, package: Package) -> Detection:
        detection = Detection(self.id, self.version, self.label, "none")
        if not package.files:
            return detection
        detection.match = "manual"
        detection.columns = {data.name: data.columns for data in package.files}
        detection.evidence.append("Any single CSV or Parquet table can be mapped by hand.")
        if len(package.files) == 1:
            data = package.files[0]
            detection.files_used.append({"name": data.name, "role": "store-day history (to be mapped)"})
            detection.proposed_mapping = {"file": data.name, **_suggest(data.columns)}
        detection.confirmations = [
            Confirmation(key="file", label="File to map"),
            Confirmation(key="date", label="Date column"),
            Confirmation(key="store_id", label="Store identifier column (or confirm a single store)", required=False),
            Confirmation(key="sales", label="Target column"),
            CONFIRM_TARGET,
            CONFIRM_FREQUENCY,
            CONFIRM_GRAIN,
            currency_confirmation(),
            Confirmation(key="date_format", label="Date format (required when dates are not YYYY-MM-DD)", options=[{"value": fmt, "label": example} for fmt, example in DATE_FORMATS.items()], required=False),
            Confirmation(key="open", label="Opening-status column (0/1, 1 = open)", required=False),
            Confirmation(key="is_closed", label="Closed-flag column (0/1, 1 = closed)", required=False),
            Confirmation(
                key="closure",
                label="Closure handling when no opening/closed column is mapped",
                options=[
                    {"value": "omit", "label": "No closure feature"},
                    {"value": "derive_platform_calendar", "label": "Treat 25 December as closed"},
                ],
                required=False,
            ),
            MISSING_DATES_CONFIRMATION,
        ]
        return detection

    def normalize(self, package: Package, config: Dict[str, Any], max_rows: int) -> Normalized:
        options = FeatureOptions(
            snap="omit",
            closure="source" if (config.get("open") or config.get("is_closed")) else (config.get("closure") or "omit"),
            missing_dates=config.get("missing_dates", "reject"),
            holiday_calendar="none",
        )
        result = Normalized(None, options, str(config.get("source_namespace") or "mapped"), str(config.get("target_type") or ""), str(config.get("source_grain") or ""))

        def need(code: str, message: str, column: Optional[str] = None, action: Optional[str] = None) -> Normalized:
            result.issues.append(Issue(code, message, stage="mapping", column=column, action=action))
            return result

        data = package.by_name(config.get("file") or (package.files[0].name if len(package.files) == 1 else ""))
        if data is None:
            return need("CONFIRMATION_REQUIRED", "Choose which uploaded file to map.")
        columns = set(data.columns)
        for key in ("date", "sales"):
            if not config.get(key):
                return need("CONFIRMATION_REQUIRED", f"Map the {key} column.", key)
            if config[key] not in columns:
                return need("MISSING_REQUIRED_COLUMN", f"{config[key]} is not a column of {data.name}.", key)
        store_column = config.get("store_id")
        single_store = (config.get("single_store_id") or "").strip()
        if store_column and store_column not in columns:
            return need("MISSING_REQUIRED_COLUMN", f"{store_column} is not a column of {data.name}.", "store_id")
        if not store_column:
            if not single_store or config.get("confirm_single_store") is not True:
                return need(
                    "CONFIRMATION_REQUIRED",
                    "Map a store column, or confirm that the whole file is one store and name it.",
                    "store_id",
                )
            if not contract.STORE_ID_PATTERN.match(single_store):
                return need("INVALID_STORE_ID", "The store name must be letters, digits, '_' or '-'.", "store_id")
        if config.get("target_type") != "revenue":
            if config.get("target_type") == "quantity":
                return need(
                    "TARGET_TYPE_MISMATCH",
                    "The target is a quantity (units). This configuration forecasts revenue; units are not "
                    "converted to money by renaming a column.",
                    "sales",
                    "Upload revenue, or train with a separate demand (quantity) configuration.",
                )
            return need("CONFIRMATION_REQUIRED", "Confirm whether the target is revenue or quantity.", "sales")
        if config.get("frequency") != "daily":
            if config.get("frequency"):
                return need("UNSUPPORTED_FREQUENCY", "Only daily data is supported by this configuration.", action="Upload one row per store per day.")
            return need("CONFIRMATION_REQUIRED", "Confirm the data frequency.")
        if config.get("source_grain") != "store_day":
            if config.get("source_grain"):
                return need(
                    "SOURCE_GRAIN_UNSUPPORTED",
                    "Transaction- and product-level rows are not aggregated automatically: there is no "
                    "documented aggregation policy for them here.",
                    action="Total the data to one row per store per day (revenue) before uploading.",
                )
            return need("CONFIRMATION_REQUIRED", "Confirm what one row represents.")
        if config.get("open") and config.get("is_closed"):
            return need("AMBIGUOUS_MAPPING", "Map either an opening-status column or a closed flag, not both.")

        mapped = [config["date"], config["sales"]] + [c for c in (store_column, config.get("open"), config.get("is_closed")) if c]
        if len(set(mapped)) != len(mapped):
            return need("AMBIGUOUS_MAPPING", "One source column is mapped to two fields.")

        samples = _date_samples(data, config["date"])
        status = date_format_status(samples)
        date_format = config.get("date_format")
        if not status["iso"]:
            if not date_format:
                return need(
                    "AMBIGUOUS_DATE_FORMAT",
                    "Dates are not YYYY-MM-DD"
                    + (f"; they could be {', '.join(DATE_FORMATS[c] for c in status['candidates'])}" if status["candidates"] else "")
                    + ". Choose the date format.",
                    "date",
                )
            if date_format not in DATE_FORMATS:
                return need("INVALID_POLICY", "Unsupported date format.", "date")

        raw = self.read_text_table(data, list(dict.fromkeys(mapped + list(data.columns))), max_rows)
        limit = self.row_limit_issue(raw, max_rows, data.name)
        if limit:
            result.issues.append(limit)
            return result
        result.rows_received = len(raw)
        dates = raw[config["date"]].astype("string").str.strip()
        if not status["iso"]:
            parsed = pd.to_datetime(dates, format=date_format, errors="coerce")
            # Unparseable values stay as written, so the validator reports them.
            dates = parsed.dt.strftime("%Y-%m-%d").astype("string").where(parsed.notna(), dates)
        frame = pd.DataFrame(
            {
                "date": dates,
                "store_id": raw[store_column].astype("string").str.strip() if store_column else single_store,
                "sales": raw[config["sales"]],
                "_source_row": raw.index + 2,
            }
        )
        transformations = [
            {"source": config["date"], "target": "date", "rule": "unchanged" if status["iso"] else f"parsed as {DATE_FORMATS[date_format]}"},
            {"source": store_column or "(confirmed single store)", "target": "store_id", "rule": "unchanged" if store_column else f'every row assigned to "{single_store}" (confirmed)'},
            {"source": config["sales"], "target": "sales", "rule": "unchanged; confirmed daily revenue"},
        ]
        if config.get("open"):
            open_text = raw[config["open"]].astype("string").str.strip()
            frame["is_closed"] = open_text.map({"1": "0", "0": "1"}).astype("string").where(open_text.isin(["0", "1"]), open_text)
            transformations.append({"source": config["open"], "target": "is_closed", "rule": "1 - open (explicit)"})
        elif config.get("is_closed"):
            frame["is_closed"] = raw[config["is_closed"]]
            transformations.append({"source": config["is_closed"], "target": "is_closed", "rule": "unchanged (0/1)"})
        else:
            transformations.append({"source": "(none)", "target": "is_closed", "rule": "omitted" if options.closure == "omit" else "platform calendar (25 December), chosen explicitly"})
        transformations.append({"source": "(none)", "target": "snap_day", "rule": "omitted: no SNAP schedule for this source"})
        transformations.append({"source": "(none)", "target": "is_holiday (model feature)", "rule": "omitted: no holiday calendar is known for this source"})

        others = [column for column in data.columns if column not in mapped]
        context = pd.DataFrame({"store_id": frame["store_id"], "date": frame["date"]})
        for column in others:
            context[column] = raw[column]
        result.retained_columns = [
            {"column": column, "file": data.name, "reason": "Not mapped to a store_day_v1 field; retained as context, not a model input."}
            for column in others
        ]
        result.frame = frame
        result.context = context if others else None
        result.transformations = transformations
        result.mapping = {key: config.get(key) for key in ("file", "date", "store_id", "sales", "open", "is_closed", "date_format", "single_store_id")}
        result.excluded_files = [{"name": other.name, "reason": "not selected for mapping"} for other in package.files if other is not data]
        if others:
            result.message = (
                f"{data.name} was mapped to store_day_v1. {len(others)} other column(s) were retained "
                "as context but are not used by this training configuration."
            )
        return result
