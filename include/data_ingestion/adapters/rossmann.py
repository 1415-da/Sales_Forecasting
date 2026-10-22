"""
Rossmann Store Sales (Kaggle) into store_day_v1.

Source semantics, documented here because nothing else records them:

* ``train.csv`` is one row per store per day; ``Sales`` is the day's turnover
  (revenue), ``Customers`` the number of customers, ``Open`` 0/1.
* ``store.csv`` is one row per store of static metadata.
* ``test.csv`` has no ``Sales`` (it is the competition's forecast period) and
  ``sample_submission.csv`` is an output template. Neither is ever training
  data.

Mapping into store_day_v1:

=============  ==============  =================================================
Source         Canonical       Rule
=============  ==============  =================================================
Store          store_id        ``"rossmann_" + Store``, read as text so store 1
                               is ``rossmann_1`` (never ``1.0``); the namespace
                               keeps Rossmann stores distinct from M5 stores
                               across training, serving, monitoring and the
                               store selector, and matches the store-id regex.
Date           date            unchanged (YYYY-MM-DD)
Sales          sales           unchanged; revenue
Open           is_closed       ``1 - Open``; values must be 0/1, blanks are an
                               error (never inferred from zero sales)
=============  ==============  =================================================

Not mapped, retained as context: DayOfWeek, Customers, Promo, StateHoliday,
SchoolHoliday, and any store.csv metadata. store_day_v1 has no fields for
them, so they are not model inputs, and no promotion or traffic scenario is
enabled because they exist. Customers is a same-day realised value, so it
could not be a known future input anyway.

SNAP is a US benefit calendar; Rossmann stores are German. It is never
derived here.
"""

from __future__ import annotations

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
from training.contract import FeatureOptions, Issue, warning

NAMESPACE = "rossmann"
STORE_PREFIX = "rossmann_"

TRAIN_REQUIRED = {"store", "date", "sales"}
TRAIN_OPTIONAL = {"open", "dayofweek", "customers", "promo", "stateholiday", "schoolholiday"}
#: Fields retained as context, with the reason each is not a model input.
CONTEXT_REASONS = {
    "dayofweek": "Derivable from date; the feature pipeline builds its own calendar features.",
    "customers": "Realised on the same day as sales, so not known in advance; store_day_v1 has no traffic field.",
    "promo": "store_day_v1 has no promotion field; a promotion-aware contract would be needed.",
    "stateholiday": "German public-holiday flags; store_day_v1 has no holiday-context field.",
    "schoolholiday": "School holidays; store_day_v1 has no field for them.",
}
STORE_FIELDS = {
    "storetype", "assortment", "competitiondistance", "competitionopensincemonth",
    "competitionopensinceyear", "promo2", "promo2sinceweek", "promo2sinceyear", "promointerval",
}
TEST_FIELDS = {"id", "store", "date"}
SUBMISSION_FIELDS = {"id", "sales"}

RETAINED_MESSAGE = (
    "Rossmann data was adapted to store_day_v1. Additional customer, promotion and "
    "holiday context was retained in the source but is not used by this training "
    "configuration."
)

CLOSURE_CONFIRMATION = Confirmation(
    key="closure",
    label="Closure handling (the file has no Open column)",
    options=[
        {"value": "omit", "label": "No closure feature (closed days are not modelled)"},
        {"value": "derive_platform_calendar", "label": "Treat 25 December as closed (platform calendar)"},
    ],
    help="Without Open, closure cannot be read from the data. Choose explicitly; zero sales is never taken to mean closed.",
)


def _role(data: DataFile) -> str:
    fields = set(data.normalized)
    if TRAIN_REQUIRED <= fields:
        return "train"
    if fields == SUBMISSION_FIELDS:
        return "submission"
    if TEST_FIELDS <= fields and "sales" not in fields:
        return "test"
    if "store" in fields and "date" not in fields and len(fields & STORE_FIELDS) >= 2:
        return "store_metadata"
    return "unknown"


class RossmannAdapter(Adapter):
    id = "rossmann"
    version = "1"
    label = "Rossmann Store Sales (train.csv, optional store.csv)"

    def _roles(self, package: Package) -> Dict[str, List[DataFile]]:
        roles: Dict[str, List[DataFile]] = {}
        for data in package.files:
            roles.setdefault(_role(data), []).append(data)
        return roles

    def detect(self, package: Package) -> Detection:
        roles = self._roles(package)
        detection = Detection(self.id, self.version, self.label, "none")
        trains = roles.get("train", [])
        if not trains and not roles.get("test") and not roles.get("store_metadata"):
            return detection
        detection.columns = {data.name: data.columns for data in package.files}
        if not trains:
            detection.match = "partial"
            detection.missing_files.append("a training file with Store, Date and Sales (train.csv)")
            if roles.get("test"):
                detection.evidence.append(
                    f"{roles['test'][0].name} looks like Rossmann test data, which has no Sales and cannot be used for supervised training."
                )
            return detection
        if len(trains) > 1:
            detection.match = "partial"
            detection.ambiguities.append(
                "Several files look like Rossmann training data: " + ", ".join(data.name for data in trains)
            )
            return detection
        train = trains[0]
        present = sorted(TRAIN_OPTIONAL & set(train.normalized))
        detection.match = "exact" if len(present) >= 3 else "partial"
        detection.evidence.append(f"{train.name} has Store, Date and Sales.")
        if present:
            detection.evidence.append(
                f"It has Rossmann fields: {', '.join(train.normalized[name] for name in present)}."
            )
        if detection.match == "partial":
            detection.evidence.append(
                "Only the generic Store/Date/Sales columns were found, so this could be another source; confirm it is Rossmann data."
            )
        detection.files_used.append({"name": train.name, "role": "training history"})
        for data in roles.get("store_metadata", []):
            detection.files_used.append({"name": data.name, "role": "store metadata (retained as context)"})
        if len(roles.get("store_metadata", [])) > 1:
            detection.ambiguities.append("More than one store metadata file was uploaded.")
        for data in roles.get("test", []):
            detection.files_excluded.append({"name": data.name, "reason": "test period without Sales; excluded from supervised training"})
        for data in roles.get("submission", []):
            detection.files_excluded.append({"name": data.name, "reason": "submission template; never used as historical sales"})
        for data in roles.get("unknown", []):
            detection.files_excluded.append({"name": data.name, "reason": "not a recognised Rossmann file"})
        detection.confirmations = [currency_confirmation(), MISSING_DATES_CONFIRMATION]
        if "open" not in train.normalized:
            detection.confirmations.append(CLOSURE_CONFIRMATION)
        if detection.match == "partial":
            detection.confirmations.append(
                Confirmation(
                    key="confirm_source",
                    label="This file is Rossmann store-day data (Sales is daily revenue per store)",
                    options=[{"value": "yes", "label": "Yes, it is Rossmann data"}],
                )
            )
        detection.proposed_mapping = {
            "store_id": f"{STORE_PREFIX}<{train.normalized['store']}>",
            "date": train.normalized["date"],
            "sales": train.normalized["sales"],
            **({"is_closed": f"1 - {train.normalized['open']}"} if "open" in train.normalized else {}),
        }
        return detection

    def normalize(self, package: Package, config: Dict[str, Any], max_rows: int) -> Normalized:
        roles = self._roles(package)
        trains = roles.get("train", [])
        has_open = bool(trains) and "open" in trains[0].normalized
        options = FeatureOptions(
            snap="omit",
            closure="source" if has_open else (config.get("closure") or "omit"),
            missing_dates=config.get("missing_dates", "reject"),
            holiday_calendar="none",
        )
        result = Normalized(None, options, NAMESPACE, "revenue", "store_day")
        result.excluded_files = (
            [{"name": data.name, "reason": "test period without Sales; excluded from supervised training"} for data in roles.get("test", [])]
            + [{"name": data.name, "reason": "submission template; never used as historical sales"} for data in roles.get("submission", [])]
            + [{"name": data.name, "reason": "not a recognised Rossmann file"} for data in roles.get("unknown", [])]
        )
        if len(trains) != 1:
            result.issues.append(
                Issue(
                    "MISSING_REQUIRED_FILE",
                    "Rossmann training needs exactly one file with Store, Date and Sales (train.csv)."
                    + (" test.csv has no Sales and cannot be trained on." if roles.get("test") else ""),
                    stage="detection",
                    action="Upload train.csv (optionally with store.csv).",
                )
            )
            return result
        if not has_open and options.closure not in contract.CLOSURE_POLICIES:
            result.issues.append(
                Issue("CONFIRMATION_REQUIRED", "Choose how closures are handled; the file has no Open column.", stage="mapping", column="open")
            )
            return result

        train = trains[0]
        wanted = [train.normalized[name] for name in sorted((TRAIN_REQUIRED | TRAIN_OPTIONAL) & set(train.normalized))]
        raw = self.read_text_table(train, wanted, max_rows)
        limit = self.row_limit_issue(raw, max_rows, train.name)
        if limit:
            result.issues.append(limit)
            return result
        raw.columns = [str(column).strip().casefold() for column in raw.columns]
        result.rows_received = len(raw)
        source_row = raw.index + 2

        store_text = raw["store"].astype("string").str.strip()
        frame = pd.DataFrame(
            {
                "date": raw["date"].astype("string").str.strip(),
                "store_id": STORE_PREFIX + store_text,
                "sales": raw["sales"],
                "_source_row": source_row,
            }
        )
        # A blank Store would otherwise become the bare prefix and pass the
        # store-id pattern; it is kept blank so the validator reports it.
        frame.loc[store_text.isna() | (store_text == ""), "store_id"] = pd.NA
        non_integer = store_text.notna() & ~store_text.fillna("").str.fullmatch(r"\d+")
        if non_integer.any():
            result.issues.append(
                Issue(
                    "INVALID_STORE_ID",
                    "Rossmann Store values must be whole numbers.",
                    int(non_integer.sum()),
                    [{"row": int(r), "Store": str(v)} for r, v in zip(source_row[non_integer][:10], store_text[non_integer][:10])],
                    stage="mapping",
                    file=train.name,
                    column=train.normalized["store"],
                )
            )

        transformations = [
            {"source": train.normalized["store"], "target": "store_id", "rule": f'"{STORE_PREFIX}" + Store (text; namespace {NAMESPACE})'},
            {"source": train.normalized["date"], "target": "date", "rule": "unchanged (YYYY-MM-DD)"},
            {"source": train.normalized["sales"], "target": "sales", "rule": "unchanged; daily store revenue (turnover)"},
        ]
        if has_open:
            open_text = raw["open"].astype("string").str.strip()
            invalid = open_text.notna() & ~open_text.isin(["0", "1"])
            blank = open_text.isna()
            if invalid.any() or blank.any():
                mask = invalid | blank
                result.issues.append(
                    Issue(
                        "INVALID_OPEN_VALUE",
                        f"Open must be 0 or 1 ({int(invalid.sum())} other value(s), {int(blank.sum())} blank). "
                        "Closure is never inferred from zero sales.",
                        int(mask.sum()),
                        [{"row": int(r), "Open": (None if pd.isna(v) else str(v))} for r, v in zip(source_row[mask][:10], open_text[mask][:10])],
                        stage="rows",
                        file=train.name,
                        column=train.normalized["open"],
                        action="Correct the Open values; blanks are not treated as open or closed.",
                    )
                )
            # Kept as text so the validator's 0/1 check sees any bad value.
            frame["is_closed"] = open_text.map({"1": "0", "0": "1"}).astype("string")
            transformations.append(
                {"source": train.normalized["open"], "target": "is_closed", "rule": "1 - Open (explicit opening status; overrides any closure calendar)"}
            )
        else:
            transformations.append(
                {
                    "source": "(none)",
                    "target": "is_closed",
                    "rule": "omitted (no closure feature)" if options.closure == "omit" else "platform calendar: 25 December closed (explicitly chosen)",
                }
            )
        transformations.append({"source": "(none)", "target": "snap_day", "rule": "omitted: SNAP is a US benefit calendar, not applicable to Rossmann"})
        transformations.append({"source": "(none)", "target": "is_holiday (model feature)", "rule": "omitted: the platform's US federal holiday calendar is not applied to German stores; StateHoliday is retained as context"})

        context_columns = sorted(set(raw.columns) & set(CONTEXT_REASONS))
        context = pd.DataFrame({"store_id": frame["store_id"], "date": frame["date"]})
        for name in context_columns:
            context[train.normalized[name]] = raw[name]
        result.retained_columns = [
            {"column": train.normalized[name], "file": train.name, "reason": CONTEXT_REASONS[name]} for name in context_columns
        ]

        metas = roles.get("store_metadata", [])
        if len(metas) > 1:
            result.issues.append(Issue("AMBIGUOUS_FILES", "More than one store metadata file was uploaded.", stage="detection"))
        elif metas:
            context, meta_report = self._join_store_metadata(metas[0], store_text, context, max_rows, result)
            result.store_metadata = meta_report

        result.frame = frame
        result.context = context
        result.transformations = transformations
        result.message = RETAINED_MESSAGE
        result.mapping = {
            "train_file": train.name,
            "store_metadata_file": metas[0].name if len(metas) == 1 else None,
            "store_id_rule": f"{STORE_PREFIX}<Store>",
        }
        return result

    def _join_store_metadata(
        self,
        meta: DataFile,
        train_store: pd.Series,
        context: pd.DataFrame,
        max_rows: int,
        result: Normalized,
    ):
        table = self.read_text_table(meta, list(meta.normalized.values()), max_rows)
        table.columns = [str(column).strip().casefold() for column in table.columns]
        keys = table["store"].astype("string").str.strip()
        duplicated = keys[keys.duplicated(keep=False)]
        report: Dict[str, Any] = {"file": meta.name, "rows": int(len(table))}
        if not duplicated.empty:
            result.issues.append(
                Issue(
                    "STORE_METADATA_DUPLICATES",
                    f"{meta.name} lists {duplicated.nunique()} store(s) more than once; it must have one row per store.",
                    int(duplicated.nunique()),
                    [{"Store": str(value)} for value in sorted(duplicated.unique())[:10]],
                    stage="mapping",
                    file=meta.name,
                    column=meta.normalized["store"],
                )
            )
            report["joined"] = False
            return context, report
        table = table.assign(_store=keys)
        fields = [name for name in table.columns if name not in ("store", "_store")]
        renamed = {name: f"store_{meta.normalized[name]}" for name in fields}
        before = len(context)
        joined = context.assign(_store=train_store.values).merge(
            table[["_store"] + fields].rename(columns=renamed), on="_store", how="left", validate="many_to_one"
        )
        if len(joined) != before:  # pragma: no cover - validate= already guarantees this
            raise AssertionError("store metadata join multiplied rows")
        train_stores = set(train_store.dropna().unique())
        meta_stores = set(keys.dropna().unique())
        unmatched_train = sorted(train_stores - meta_stores, key=lambda value: (len(value), value))
        unmatched_meta = sorted(meta_stores - train_stores, key=lambda value: (len(value), value))
        report.update(
            {
                "joined": True,
                "relationship": "many-to-one (validated)",
                "rows_before": before,
                "rows_after": int(len(joined)),
                "stores_in_metadata": len(meta_stores),
                "train_stores_without_metadata": unmatched_train[:50],
                "metadata_stores_without_history": unmatched_meta[:50],
                "fields_retained": [meta.normalized[name] for name in fields],
            }
        )
        if unmatched_train:
            result.issues.append(
                warning(
                    "UNMATCHED_STORE_METADATA",
                    f"{len(unmatched_train)} store(s) in the training file have no row in {meta.name}.",
                    count=len(unmatched_train),
                    stage="mapping",
                    file=meta.name,
                )
            )
        result.retained_columns.extend(
            {"column": meta.normalized[name], "file": meta.name, "reason": "Static store metadata; store_day_v1 has no field for it."}
            for name in fields
        )
        return joined.drop(columns=["_store"]), report
