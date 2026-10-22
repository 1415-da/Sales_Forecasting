"""
Training charts on multi-store data.

The residual and error charts once joined actuals to predictions on ``date``
alone: with N stores per day that is N x N rows per day, which inflated the
charts and ran a 935-store training run out of memory. Runs where the training
image's plotting libraries exist (the Airflow image).
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("seaborn")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "include"))

from ml_models.model_visualization import ModelVisualizer  # noqa: E402


def frames(stores=50, days=30):
    test = pd.DataFrame(
        [{"date": d, "store_id": s, "sales": float(s * 10 + n)} for n, d in enumerate(pd.date_range("2016-01-01", periods=days)) for s in range(stores)]
    )
    predictions = test[["date"]].copy()
    predictions["prediction"] = test["sales"] + 1.0
    return test, predictions


def test_actuals_and_predictions_align_row_for_row():
    test, predictions = frames()
    aligned = ModelVisualizer._aligned(test, predictions, "sales")
    assert len(aligned) == len(test)  # not stores**2 * days
    assert np.allclose(aligned["sales"] - aligned["prediction"], -1.0)


def test_residual_and_error_charts_render_for_many_stores(tmp_path):
    test, predictions = frames(stores=300, days=40)
    visualizer = ModelVisualizer()
    visualizer.create_residuals_analysis({"ensemble": predictions}, test, save_path=str(tmp_path / "r.png"))
    visualizer.create_error_distribution_chart({"ensemble": predictions}, test, save_path=str(tmp_path / "e.png"))
    assert (tmp_path / "r.png").exists() and (tmp_path / "e.png").exists()
