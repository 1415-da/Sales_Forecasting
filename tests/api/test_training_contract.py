"""
The store-day training contract: schema, validation, hashing, combination.

Pure functions over frames, so every rule is pinned with a small hand-built
dataset rather than a fixture file nobody reads.
"""

from __future__ import annotations

import io
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "include"))

from training import contract  # noqa: E402

POLICY = contract.ValidationPolicy(warmup_days=30, min_train_days_after_warmup=30)


def store_days(stores=("CA_1", "TX_1"), start="2015-01-01", days=200, base=1000.0) -> pd.DataFrame:
    rows = []
    for index, store in enumerate(stores):
        for day in pd.date_range(start, periods=days, freq="D"):
            # Keyed on the calendar day, so two files covering a day agree.
            offset = (day - pd.Timestamp("2015-01-01")).days
            rows.append(
                {"date": day.strftime("%Y-%m-%d"), "store_id": store, "sales": base + index * 100 + offset}
            )
    return pd.DataFrame(rows)


def as_raw(frame: pd.DataFrame) -> pd.DataFrame:
    """The frame as CSV text cells, the way an upload arrives."""
    return contract.read_table(frame.to_csv(index=False).encode(), "csv")


def check(frame: pd.DataFrame, policy=POLICY, **kwargs):
    return contract.validate(as_raw(frame), policy, currency=kwargs.pop("currency", "USD"), source_type="real", **kwargs)


def codes(result, kind="errors"):
    return set(contract.iter_issue_codes(result.report, kind))


# -- schema ------------------------------------------------------------------


def test_a_clean_store_day_file_validates():
    result = check(store_days())
    assert result.is_valid, result.report["errors"]
    assert result.report["rows_valid"] == 400
    assert result.report["store_count"] == 2
    assert result.report["split"]["valid"]


def test_missing_required_columns_are_named():
    result = check(store_days().drop(columns=["sales"]))
    assert not result.is_valid
    assert "missing_columns" in codes(result)


def test_product_level_rows_are_refused_not_summed():
    frame = store_days()
    frame["product_id"] = "FOODS_1_001"
    result = check(frame)
    assert "product_grain" in codes(result)
    assert not result.is_valid


def test_an_unmapped_context_column_is_refused():
    frame = store_days()
    frame["promo_flag"] = 1
    assert "unmapped_columns" in codes(check(frame))


def test_dates_must_be_iso_calendar_days():
    frame = store_days()
    frame.loc[3, "date"] = "03/04/2015"
    frame.loc[4, "date"] = "2015-02-30"
    result = check(frame)
    issue = next(item for item in result.report["errors"] if item["code"] == "invalid_date")
    assert issue["count"] == 2
    assert issue["examples"][0]["row"] == 5  # header is line 1


def test_a_blank_target_is_an_error_not_a_zero():
    frame = store_days()
    frame["sales"] = frame["sales"].astype(object)
    frame.loc[10, "sales"] = ""
    assert "null_target" in codes(check(frame))


def test_negative_revenue_follows_the_returns_policy():
    frame = store_days()
    frame.loc[50, "sales"] = -5
    assert "negative_sales" in codes(check(frame))
    allowing = contract.ValidationPolicy(negative_sales="allow")
    result = check(frame, policy=allowing)
    assert result.is_valid
    assert "negative_sales" in codes(result, "warnings")


def test_store_identifiers_are_checked_and_scoped():
    frame = store_days()
    frame.loc[0, "store_id"] = "../etc"
    assert "invalid_store_id" in codes(check(frame))
    scoped = contract.ValidationPolicy(allowed_stores=("CA_1",))
    assert "store_out_of_scope" in codes(check(store_days(), policy=scoped))


def test_an_unsupported_currency_is_a_different_target():
    assert "incompatible_currency" in codes(check(store_days(), currency="EUR"))


# -- grain -------------------------------------------------------------------


def test_identical_repeated_rows_are_collapsed_and_counted():
    frame = pd.concat([store_days(), store_days().head(5)], ignore_index=True)
    result = check(frame)
    assert result.is_valid
    assert result.report["exact_duplicates_removed"] == 5
    assert len(result.frame) == 400


def test_conflicting_store_days_are_rejected_with_examples():
    extra = store_days().head(3).assign(sales=1.0)
    result = check(pd.concat([store_days(), extra], ignore_index=True))
    issue = next(item for item in result.report["errors"] if item["code"] == "conflicting_duplicates")
    assert issue["count"] == 3
    assert issue["examples"]


def test_a_gap_is_missing_dates_not_a_zero_sales_day():
    frame = store_days()
    gap = frame.drop(index=[20, 21])
    result = check(gap)
    assert "missing_dates" in codes(result)
    zeros = store_days()
    zeros.loc[[20, 21], "sales"] = 0
    result = check(zeros)
    assert result.is_valid
    assert next(item for item in result.report["stores"] if item["store_id"] == "CA_1")["zero_sales_days"] == 2


def test_history_minimums_come_from_the_feature_definitions():
    assert contract.feature_warmup_days() == 30  # max lag / window in ml_config.yaml
    result = check(store_days(days=50))
    assert {"insufficient_history", "insufficient_dates"} <= codes(result)


def test_an_append_upload_defers_history_rules_to_the_combined_data():
    short = store_days(days=20)
    result = check(short, require_history=False)
    assert result.is_valid
    assert check(short).is_valid is False


def test_the_split_never_puts_one_day_in_two_sets():
    split = contract.split_dates(pd.date_range("2015-01-01", periods=200), POLICY)
    assert split["train"][1] < split["validation"][0]
    assert split["validation"][1] < split["test"][0]
    assert split["train_days"] + split["validation_days"] + split["test_days"] == 200


# -- context features --------------------------------------------------------


def test_snap_is_derived_from_the_same_schedule_serving_uses():
    from api.services.feature_service import SNAP_DAYS_BY_STATE

    assert contract.SNAP_DAYS_BY_STATE == SNAP_DAYS_BY_STATE
    result = check(store_days())
    assert result.report["context_features"]["snap_day"].startswith("derived")
    ca_first = result.frame[(result.frame["store_id"] == "CA_1")].iloc[0]
    assert ca_first["snap_day"] == 1  # CA issues on days 1-10


def test_snap_for_an_unknown_state_is_left_out_not_invented():
    result = check(store_days(stores=("STORE-9", "STORE-10")))
    assert result.is_valid
    assert "snap_day" not in result.frame.columns
    assert "snap_unavailable" in codes(result, "warnings")


def test_a_partly_filled_context_column_is_refused():
    frame = store_days()
    frame["snap_day"] = 0
    frame["snap_day"] = frame["snap_day"].astype(object)
    frame.loc[5, "snap_day"] = "maybe"
    assert "invalid_snap_day" in codes(check(frame))


# -- files and hashing -------------------------------------------------------


@pytest.mark.parametrize(
    "name,content",
    [
        ("model.pkl", b"\x80\x04K\x01."),
        ("model.joblib", b"\x80\x04K\x01."),
        ("renamed.parquet", b"date,store_id,sales\n"),
    ],
)
def test_only_real_csv_or_parquet_files_are_read(name, content):
    with pytest.raises(contract.UploadReadError):
        contract.detect_format(name, content)


def test_the_content_hash_ignores_format_and_row_order():
    result = check(store_days())
    shuffled = result.frame.sample(frac=1.0, random_state=7)
    assert contract.content_hash(result.frame) == contract.content_hash(shuffled)
    round_trip = contract.from_parquet_bytes(contract.to_parquet_bytes(result.frame))
    assert contract.content_hash(round_trip) == contract.content_hash(result.frame)
    parquet = io.BytesIO()
    store_days().to_parquet(parquet, index=False)
    from_parquet = contract.validate(
        contract.read_table(parquet.getvalue(), "parquet"), POLICY, currency="USD", source_type="real"
    )
    assert contract.content_hash(from_parquet.frame) == contract.content_hash(result.frame)


def test_a_one_cent_change_changes_the_hash():
    frame = check(store_days()).frame
    changed = frame.copy()
    changed.loc[0, "sales"] += 0.01
    assert contract.content_hash(frame) != contract.content_hash(changed)


# -- combination -------------------------------------------------------------


META = {"schema_version": contract.SCHEMA_VERSION, "target": "sales", "frequency": "D", "currency": "USD", "source_types": ["real"]}


def test_appending_removes_exact_overlap_and_keeps_inputs_unchanged():
    base = check(store_days(days=200)).frame
    new = check(store_days(start="2015-07-10", days=30), require_history=False).frame
    base_copy, new_copy = base.copy(), new.copy()
    result = contract.combine(base, META, new, META, conflict_policy="reject")
    assert result.frame is not None
    overlap = 200 - pd.Timestamp("2015-07-10").dayofyear + 1
    assert result.report["exact_duplicates_removed"] == overlap * 2
    assert result.report["rows"] == len(base) + len(new) - overlap * 2
    pd.testing.assert_frame_equal(base, base_copy)
    pd.testing.assert_frame_equal(new, new_copy)


def test_conflicting_overlap_is_rejected_unless_correction_is_chosen():
    base = check(store_days(days=200)).frame
    new = check(store_days(start="2015-07-10", days=30, base=5.0), require_history=False).frame
    rejected = contract.combine(base, META, new, META, conflict_policy="reject")
    assert rejected.frame is None
    assert rejected.report["conflicts"] > 0
    corrected = contract.combine(base, META, new, META, conflict_policy="prefer_new")
    assert corrected.report["corrections_applied"] == rejected.report["conflicts"]
    day = corrected.frame[(corrected.frame["store_id"] == "CA_1") & (corrected.frame["date"] == pd.Timestamp("2015-07-10"))]
    assert day["sales"].iloc[0] == new.iloc[0]["sales"]


def test_real_and_synthetic_records_are_never_blended():
    base = check(store_days()).frame
    synthetic = dict(META, source_types=["synthetic"])
    result = contract.combine(base, META, base, synthetic, conflict_policy="reject")
    assert result.frame is None
    assert any(item["code"] == "mixed_sources" for item in result.report["errors"])


def test_appending_a_different_store_domain_or_currency_is_refused():
    base = check(store_days(stores=("CA_1",))).frame
    other = check(store_days(stores=("WI_1",))).frame
    assert contract.combine(base, META, other, META, conflict_policy="reject").frame is None
    euro = dict(META, currency="EUR")
    result = contract.combine(base, META, base, euro, conflict_policy="reject")
    assert any(item["code"] == "incompatible_currency" for item in result.report["errors"])
