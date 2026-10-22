"""
Ingestion: package inspection, adapter detection, normalization, validation.

Small generated fixtures shaped like the real sources (Rossmann's train.csv,
store.csv, test.csv and sample_submission.csv; a differently named store-day
table) so each rule is pinned without shipping the 38 MB original.
"""

from __future__ import annotations

import io
import os
import stat
import sys
import zipfile

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "include"))

from data_ingestion import adapter_registry, pipeline  # noqa: E402
from data_ingestion.limits import UploadLimits  # noqa: E402
from data_ingestion.package_inspector import StagedFile, inspect_package  # noqa: E402
from training import contract  # noqa: E402

LIMITS = UploadLimits()
POLICY = contract.ValidationPolicy()


def rossmann_train(stores=(1, 2, 3), days=200, start="2015-01-01", gap_store=None, header=None) -> pd.DataFrame:
    rows = []
    for store in stores:
        for day in pd.date_range(start, periods=days):
            if gap_store == store and pd.Timestamp("2015-03-01") <= day <= pd.Timestamp("2015-03-10"):
                continue
            open_ = 0 if day.dayofweek == 6 else 1
            rows.append(
                {
                    "Store": store,
                    "DayOfWeek": day.dayofweek + 1,
                    "Date": day.strftime("%Y-%m-%d"),
                    "Sales": (5000 + store * 10 + day.day) * open_,
                    "Customers": 500 * open_,
                    "Open": open_,
                    "Promo": int(day.day % 7 == 0),
                    "StateHoliday": "0",
                    "SchoolHoliday": 0,
                }
            )
    frame = pd.DataFrame(rows)
    if header:
        frame.columns = header
    return frame


def rossmann_store(stores=(1, 2, 3), duplicate=None) -> pd.DataFrame:
    rows = [{"Store": s, "StoreType": "a", "Assortment": "c", "CompetitionDistance": 100 * s, "Promo2": 0} for s in stores]
    if duplicate:
        rows.append({"Store": duplicate, "StoreType": "b", "Assortment": "a", "CompetitionDistance": 1, "Promo2": 1})
    return pd.DataFrame(rows)


def write(tmp_path, name, frame: pd.DataFrame | None = None, content: bytes | None = None) -> StagedFile:
    path = tmp_path / f"in-{name}"
    if frame is not None:
        if name.endswith(".parquet"):
            frame.to_parquet(path, index=False)
        else:
            frame.to_csv(path, index=False)
    else:
        path.write_bytes(content or b"")
    return StagedFile(name, str(path), path.stat().st_size, "0" * 64)


def run(tmp_path, staged, config, **kwargs):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    package = inspect_package(staged, str(work), kwargs.pop("limits", LIMITS))
    return pipeline.process(package, config, POLICY, max_rows=LIMITS.max_rows, source_type="real", **kwargs)


def codes(result, kind="errors"):
    return {issue["code"] for issue in result.report[kind]}


# -- canonical stays as it was ----------------------------------------------


def canonical(days=200) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": d.strftime("%Y-%m-%d"), "store_id": s, "sales": 100.0 + n}
            for s in ("CA_1", "TX_1")
            for n, d in enumerate(pd.date_range("2015-01-01", periods=days))
        ]
    )


@pytest.mark.parametrize("name", ["sales.csv", "sales.parquet"])
def test_canonical_csv_and_parquet_are_still_accepted(tmp_path, name):
    result = run(tmp_path, [write(tmp_path, name, canonical())], {"currency": "USD"})
    assert result.status == "validated", result.report["errors"]
    assert result.report["detection"]["selected"] == "canonical_store_day"
    assert result.report["context_features"]["snap_day"].startswith("derived")
    assert "is_closed" in result.frame.columns  # platform calendar, as before


def test_canonical_upload_without_currency_asks_instead_of_assuming(tmp_path):
    result = run(tmp_path, [write(tmp_path, "sales.csv", canonical())], {})
    assert result.status == "needs_mapping"
    assert result.report["detection"]["needs_input"] == ["currency"]
    assert result.report["rows_valid"] is None and result.report["rows_invalid"] is None
    assert result.report["stages"]["rows"]["status"] == "not_run"


# -- Rossmann ---------------------------------------------------------------


def test_rossmann_train_is_detected_and_adapted(tmp_path):
    result = run(tmp_path, [write(tmp_path, "train.csv", rossmann_train())], {"currency": "EUR"})
    assert result.status == "validated", result.report["errors"]
    assert result.report["detection"]["selected"] == "rossmann"
    frame = result.frame
    assert list(frame.columns) == ["date", "store_id", "sales", "is_closed"]
    assert sorted(frame["store_id"].unique()) == ["rossmann_1", "rossmann_2", "rossmann_3"]
    assert "snap_day" not in frame.columns  # never derived for Rossmann
    assert result.report["adapter_message"].startswith("Rossmann data was adapted to store_day_v1.")
    retained = {item["column"] for item in result.report["retained_columns"]}
    assert retained == {"DayOfWeek", "Customers", "Promo", "StateHoliday", "SchoolHoliday"}
    assert set(result.context.columns) >= retained
    assert result.normalized.options.holiday_calendar == "none"


def test_open_is_inverted_and_overrides_the_closure_calendar(tmp_path):
    train = rossmann_train(start="2014-12-20", days=200)
    train.loc[(train["Store"] == 1) & (train["Date"] == "2014-12-25"), ["Open", "Sales"]] = [1, 999]
    result = run(tmp_path, [write(tmp_path, "train.csv", train)], {"currency": "EUR"})
    frame = result.frame
    christmas = frame[(frame["store_id"] == "rossmann_1") & (frame["date"] == pd.Timestamp("2014-12-25"))]
    assert christmas["is_closed"].iloc[0] == 0  # Open=1 wins over 25 December
    sunday = frame[(frame["date"].dt.dayofweek == 6)]
    assert set(sunday["is_closed"]) == {1}
    assert result.report["context_features"]["is_closed"] == "provided"


def test_closure_is_never_inferred_from_zero_sales(tmp_path):
    train = rossmann_train()
    train.loc[(train["Store"] == 2) & (train["Date"] == "2015-02-03"), "Sales"] = 0  # open, sold nothing
    result = run(tmp_path, [write(tmp_path, "train.csv", train)], {"currency": "EUR"})
    row = result.frame[(result.frame["store_id"] == "rossmann_2") & (result.frame["date"] == pd.Timestamp("2015-02-03"))]
    assert row["is_closed"].iloc[0] == 0


@pytest.mark.parametrize("bad", ["2", "yes", ""])
def test_invalid_or_blank_open_values_are_reported(tmp_path, bad):
    train = rossmann_train().astype({"Open": object})
    train.loc[5, "Open"] = bad
    result = run(tmp_path, [write(tmp_path, "train.csv", train)], {"currency": "EUR"})
    assert result.status == "invalid"
    assert "INVALID_OPEN_VALUE" in codes(result)


def test_column_names_match_after_trimming_and_case_folding(tmp_path):
    header = [" store ", "DAYOFWEEK", "date", "SALES", "customers", " Open", "PROMO", "stateholiday", "SchoolHoliday "]
    result = run(tmp_path, [write(tmp_path, "data.csv", rossmann_train(header=header))], {"currency": "EUR"})
    assert result.status == "validated", result.report["errors"]
    assert result.report["detection"]["selected"] == "rossmann"


def test_columns_identical_after_normalization_are_ambiguous(tmp_path):
    train = rossmann_train()
    train["SALES"] = train["Sales"]
    result = run(tmp_path, [write(tmp_path, "train.csv", train)], {"currency": "EUR"})
    assert result.status == "invalid"
    assert "AMBIGUOUS_MAPPING" in codes(result)


def test_store_ids_stay_whole_and_valid_even_from_parquet(tmp_path):
    train = rossmann_train()
    train["Store"] = train["Store"].astype("int64")
    result = run(tmp_path, [write(tmp_path, "train.parquet", train)], {"currency": "EUR"})
    ids = set(result.frame["store_id"])
    assert ids == {"rossmann_1", "rossmann_2", "rossmann_3"}
    assert all(contract.STORE_ID_PATTERN.match(value) for value in ids)


def test_store_metadata_joins_many_to_one_without_multiplying_rows(tmp_path):
    staged = [write(tmp_path, "train.csv", rossmann_train()), write(tmp_path, "store.csv", rossmann_store(stores=(1, 2, 3, 4)))]
    result = run(tmp_path, staged, {"currency": "EUR"})
    join = result.report["store_metadata"]
    assert join["rows_before"] == join["rows_after"] == 600
    assert join["relationship"].startswith("many-to-one")
    assert join["metadata_stores_without_history"] == ["4"]
    assert len(result.context) == len(result.frame)
    assert "store_StoreType" in result.context.columns


def test_duplicate_store_metadata_is_rejected(tmp_path):
    staged = [write(tmp_path, "train.csv", rossmann_train()), write(tmp_path, "store.csv", rossmann_store(duplicate=2))]
    result = run(tmp_path, staged, {"currency": "EUR"})
    assert "STORE_METADATA_DUPLICATES" in codes(result)
    assert result.status == "invalid"


def test_unmatched_store_metadata_is_a_warning(tmp_path):
    staged = [write(tmp_path, "train.csv", rossmann_train()), write(tmp_path, "store.csv", rossmann_store(stores=(1, 2)))]
    result = run(tmp_path, staged, {"currency": "EUR"})
    assert "UNMATCHED_STORE_METADATA" in codes(result, "warnings")


def test_test_and_submission_files_are_never_training_data(tmp_path):
    test = rossmann_train().drop(columns=["Sales", "Customers"]).assign(Id=range(600))
    submission = pd.DataFrame({"Id": range(10), "Sales": 0})
    only_test = run(tmp_path, [write(tmp_path, "test.csv", test), write(tmp_path, "sample_submission.csv", submission)], {"currency": "EUR"})
    assert only_test.status == "invalid"
    assert "MISSING_REQUIRED_FILE" in codes(only_test)
    full = run(
        tmp_path,
        [write(tmp_path, "train.csv", rossmann_train()), write(tmp_path, "test.csv", test), write(tmp_path, "sample_submission.csv", submission)],
        {"currency": "EUR"},
    )
    excluded = {item["name"]: item["reason"] for item in full.report["files"]["excluded"]}
    assert "test.csv" in excluded and "sample_submission.csv" in excluded
    assert len(full.frame) == 600


def test_gaps_need_an_explicit_policy_and_excluded_stores_are_listed(tmp_path):
    staged = [write(tmp_path, "train.csv", rossmann_train(gap_store=2))]
    rejected = run(tmp_path, staged, {"currency": "EUR"})
    assert "MISSING_DATES" in codes(rejected)
    excluded = run(tmp_path, staged, {"currency": "EUR", "missing_dates": "exclude_stores"})
    assert excluded.status == "validated"
    assert excluded.report["stores_excluded"] == ["rossmann_2"]
    warning = next(item for item in excluded.report["warnings"] if item["code"] == "STORES_EXCLUDED")
    assert warning["requires_ack"] is True
    assert "rossmann_2" not in set(excluded.context["store_id"])


def test_without_open_the_closure_policy_must_be_chosen(tmp_path):
    train = rossmann_train().drop(columns=["Open"])
    asked = run(tmp_path, [write(tmp_path, "train.csv", train)], {"currency": "EUR"})
    assert asked.status == "needs_mapping"
    assert "closure" in asked.report["detection"]["needs_input"]
    chosen = run(tmp_path, [write(tmp_path, "train.csv", train)], {"currency": "EUR", "closure": "omit"})
    assert chosen.status == "validated"
    assert "is_closed" not in chosen.frame.columns


def test_per_store_minimum_history_is_checked(tmp_path):
    train = pd.concat([rossmann_train(stores=(1, 2)), rossmann_train(stores=(3,), days=40, start="2015-06-01")])
    result = run(tmp_path, [write(tmp_path, "train.csv", train)], {"currency": "EUR"})
    issue = next(item for item in result.report["errors"] if item["code"] == "INSUFFICIENT_HISTORY")
    assert issue["examples"][0]["store_id"] == "rossmann_3"


def test_duplicates_and_conflicts_are_found_across_the_whole_file(tmp_path):
    train = rossmann_train()
    late = train.tail(1).copy()
    early = train.head(1).copy()
    early["Sales"] = early["Sales"] + 1  # conflict with row 2, found hundreds of rows later
    result = run(tmp_path, [write(tmp_path, "train.csv", pd.concat([train, late, early]))], {"currency": "EUR"})
    assert result.report["exact_duplicates_removed"] == 1
    assert "CONFLICTING_RECORDS" in codes(result)


# -- generic mapping ----------------------------------------------------------


def branch_table(date_format="%Y-%m-%d") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"transaction_date": d.strftime(date_format), "branch_id": b, "daily_revenue": 200.0 + n, "region": "north"}
            for b in ("B01", "B02")
            for n, d in enumerate(pd.date_range("2015-01-01", periods=200))
        ]
    )


MAPPING = {
    "adapter_id": "mapped_tabular",
    "date": "transaction_date",
    "store_id": "branch_id",
    "sales": "daily_revenue",
    "target_type": "revenue",
    "frequency": "daily",
    "source_grain": "store_day",
    "currency": "GBP",
}


def test_unknown_columns_get_suggestions_but_no_assumptions(tmp_path):
    result = run(tmp_path, [write(tmp_path, "branches.csv", branch_table())], {})
    assert result.status == "needs_mapping"
    candidates = {item["adapter_id"]: item for item in result.report["detection"]["candidates"]}
    proposed = candidates["mapped_tabular"]["proposed_mapping"]
    assert proposed["store_id"] == "branch_id" and proposed["sales"] == "daily_revenue"
    assert result.report["rows_valid"] is None


def test_a_confirmed_mapping_validates_and_retains_extra_columns(tmp_path):
    result = run(tmp_path, [write(tmp_path, "branches.csv", branch_table())], MAPPING)
    assert result.status == "validated", result.report["errors"]
    assert set(result.frame["store_id"]) == {"B01", "B02"}
    assert [item["column"] for item in result.report["retained_columns"]] == ["region"]


def test_a_quantity_target_is_refused_for_the_revenue_configuration(tmp_path):
    result = run(tmp_path, [write(tmp_path, "branches.csv", branch_table())], {**MAPPING, "target_type": "quantity"})
    assert result.status == "invalid"
    assert "TARGET_TYPE_MISMATCH" in codes(result)


def test_ambiguous_dates_need_a_chosen_format(tmp_path):
    # 01/01/2015 .. 12/01/2015 parse as day-first and month-first alike.
    ambiguous = branch_table("%d/%m/%Y").groupby("branch_id").head(12)
    asked = run(tmp_path, [write(tmp_path, "branches.csv", ambiguous)], MAPPING)
    issue = next(item for item in asked.report["errors"] if item["code"] == "AMBIGUOUS_DATE_FORMAT")
    assert "31/01/2025" in issue["message"] and "01/31/2025" in issue["message"]
    chosen = run(tmp_path, [write(tmp_path, "branches.csv", branch_table("%d/%m/%Y"))], {**MAPPING, "date_format": "%d/%m/%Y"})
    assert chosen.status == "validated", chosen.report["errors"]
    assert chosen.frame["date"].min() == pd.Timestamp("2015-01-01")


def test_a_single_store_needs_explicit_confirmation(tmp_path):
    table = branch_table()
    table = table[table["branch_id"] == "B01"].drop(columns=["branch_id"])
    config = {**MAPPING, "store_id": None}
    asked = run(tmp_path, [write(tmp_path, "one.csv", table)], config)
    assert asked.status != "validated"
    confirmed = run(tmp_path, [write(tmp_path, "one.csv", table)], {**config, "single_store_id": "shop_1", "confirm_single_store": True})
    assert confirmed.status == "validated"
    assert set(confirmed.frame["store_id"]) == {"shop_1"}


def test_transaction_level_rows_are_not_aggregated(tmp_path):
    result = run(tmp_path, [write(tmp_path, "branches.csv", branch_table())], {**MAPPING, "source_grain": "transaction"})
    assert "SOURCE_GRAIN_UNSUPPORTED" in codes(result)


def test_two_exact_adapters_require_a_choice():
    class Fake:
        def __init__(self, adapter_id):
            self.adapter_id, self.match, self.usable, self.evidence, self.label = adapter_id, "exact", True, ["x"], adapter_id

    choice = adapter_registry.choose([Fake("a"), Fake("b")])
    assert choice["selected"] is None and "Choose" in choice["reason"]


# -- packages ---------------------------------------------------------------


def make_zip(tmp_path, members, name="data.zip") -> StagedFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, content in members:
            if isinstance(member, zipfile.ZipInfo):
                archive.writestr(member, content)
            else:
                archive.writestr(member, content)
    return write(tmp_path, name, content=buffer.getvalue())


def test_a_zip_with_an_enclosing_folder_is_classified(tmp_path):
    train = rossmann_train().to_csv(index=False)
    staged = make_zip(
        tmp_path,
        [
            ("rossmann/train.csv", train),
            ("rossmann/store.csv", rossmann_store().to_csv(index=False)),
            ("rossmann/README.txt", "about"),
            ("__MACOSX/rossmann/._train.csv", "meta"),
            ("rossmann/.DS_Store", "meta"),
        ],
    )
    result = run(tmp_path, [staged], {"currency": "EUR"})
    assert result.status == "validated", result.report["errors"]
    excluded = {item["name"]: item["reason"] for item in result.report["files"]["excluded"]}
    assert excluded["README.txt"] == "not a CSV or Parquet file"
    assert excluded[".DS_Store"] == "operating-system metadata"


@pytest.mark.parametrize(
    "member",
    ["../evil.csv", "/abs.csv", "a/b/c/deep.csv", "model.pkl", "inner.zip"],
)
def test_unsafe_archives_are_rejected(tmp_path, member):
    result = run(tmp_path, [make_zip(tmp_path, [(member, "date,store_id,sales\n")])], {"currency": "EUR"})
    assert result.status == "invalid"
    assert codes(result) & {"UNSAFE_ARCHIVE", "UNSUPPORTED_ARCHIVE_LAYOUT"}


def test_symlinks_in_archives_are_rejected(tmp_path):
    info = zipfile.ZipInfo("link.csv")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    result = run(tmp_path, [make_zip(tmp_path, [(info, "/etc/passwd")])], {"currency": "EUR"})
    assert "UNSAFE_ARCHIVE" in codes(result)


def test_expanded_size_and_compression_ratio_are_limited(tmp_path):
    bomb = "0," * (3 * 1024 * 1024)
    tight = UploadLimits(max_expanded_bytes=1024 * 1024)
    result = run(tmp_path, [make_zip(tmp_path, [("big.csv", bomb)])], {"currency": "EUR"}, limits=tight)
    assert codes(result) & {"UNSAFE_ARCHIVE", "UPLOAD_TOO_LARGE"}


def test_file_content_is_checked_not_just_the_extension(tmp_path):
    pickled = write(tmp_path, "data.csv", content=b"\x80\x04\x95\x00\x00")
    fake_parquet = write(tmp_path, "data.parquet", content=b"date,store_id,sales\n")
    for staged in (pickled, fake_parquet):
        result = run(tmp_path, [staged], {"currency": "EUR"})
        assert "UNSUPPORTED_FILE_TYPE" in codes(result)


def test_schema_failures_leave_row_counts_null(tmp_path):
    frame = canonical().rename(columns={"store_id": "shop"})
    result = run(tmp_path, [write(tmp_path, "sales.csv", frame)], {"currency": "USD", "adapter_id": "canonical_store_day"})
    report = result.report
    assert report["rows_valid"] is None and report["rows_invalid"] is None
    assert report["row_validation_performed"] is False


def test_stores_with_gaps_are_marked_insufficient_in_the_store_table(tmp_path):
    result = run(tmp_path, [write(tmp_path, "train.csv", rossmann_train(gap_store=2))], {"currency": "EUR"})
    rows = {item["store_id"]: item for item in result.report["stores"]}
    assert rows["rossmann_2"]["sufficient"] is False and rows["rossmann_2"]["missing_dates"] == 10
    assert rows["rossmann_1"]["sufficient"] is True
