"""
Training-side feature contract.

These pin the two properties the serving feature builder relies on: rolling
windows end the day before the row (no same-day target), and a closed day is
not also flagged as a holiday.
"""

import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "include"))

from feature_engineering.feature_pipeline import FeatureEngineer  # noqa: E402

CONFIG = os.path.join(ROOT, "include", "config", "ml_config.yaml")


@pytest.fixture
def frame() -> pd.DataFrame:
    dates = pd.date_range("2015-12-20", periods=10, freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "store_id": "CA_1",
            "sales": [float(value) for value in range(100, 110)],
            "is_closed": [(d.month, d.day) == (12, 25) for d in dates],
        }
    ).astype({"is_closed": int})


def test_rolling_windows_exclude_the_current_day(frame):
    out = FeatureEngineer(CONFIG).create_rolling_features(frame, "sales", ["store_id"])
    # Row 5 (sales 105): the 3-day window is rows 2-4, i.e. 102, 103, 104.
    assert out.loc[5, "sales_rolling_3_mean"] == pytest.approx(103.0)
    assert out.loc[5, "sales_rolling_3_max"] == pytest.approx(104.0)
    # The first row has no prior day at all.
    assert pd.isna(out.loc[0, "sales_rolling_3_mean"])


def test_a_closed_day_is_not_also_a_holiday(frame):
    out = FeatureEngineer(CONFIG).create_date_features(frame, "date")
    christmas = out[out["date"] == pd.Timestamp("2015-12-25")].iloc[0]
    assert christmas["is_closed"] == 1
    assert christmas["is_holiday"] == 0
