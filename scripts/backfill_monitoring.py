"""
Backtest the Production model over the monitoring windows, then re-evaluate.

Monitoring scores forecasts against actuals for the Production version only,
and drift compares two adjacent windows. The M5 actuals end on 2016-04-24, so
a newly promoted model has no forecasts dated inside those windows and the
Monitoring page stays empty until it does.

This walks both windows (2 x window_days ending at the latest actual) in
weekly steps. For each store it asks the API for a 7-day forecast from the 120
days of history that precede the step -- the model never sees the days it is
scored on -- and finally calls POST /v1/monitoring/evaluate.

Runs inside the API container, which has pandas and the dataset mounted:

    docker exec -i -e API=http://localhost:8000 \\
        -e EMAIL="$MONITORING_SERVICE_EMAIL" -e PASSWORD="$MONITORING_SERVICE_PASSWORD" \\
        <project>-api-1 python - < scripts/backfill_monitoring.py
"""
import glob
import json
import os
import urllib.request
from datetime import timedelta

import pandas as pd

API = os.environ.get("API", "http://localhost:8000").rstrip("/")
SALES_ROOT = os.environ.get(
    "SALES_ROOT", "/usr/local/airflow/data/raw/kaggle_m5/v1.0.0/sales"
)
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "28"))
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "120"))
STEP_DAYS = 7


def call(path, body=None, token=None):
    request = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def store_days(start, end):
    """Daily store totals, aggregated the way the monitoring actuals are."""
    frames = []
    for path in sorted(glob.glob(os.path.join(SALES_ROOT, "**", "*.parquet"), recursive=True)):
        day = pd.Timestamp(os.path.basename(path)[len("sales_"):-len(".parquet")]).date()
        if start <= day <= end:
            frames.append(pd.read_parquet(path))
    data = pd.concat(frames, ignore_index=True)
    data["date"] = pd.to_datetime(data["date"]).dt.date
    data["promo"] = data["discount_percent"].fillna(0) > 0
    return data.groupby(["store_id", "date"], as_index=False).agg(
        sales=("revenue", "sum"),
        quantity_sold=("quantity_sold", "sum"),
        profit=("profit", "sum"),
        has_promotion=("promo", "any"),
    )


def main():
    token = call(
        "/auth/login",
        {"email": os.environ["EMAIL"], "password": os.environ["PASSWORD"]},
    )["access_token"]
    production = call("/health")["production_model"]
    model, version = production["model"], production["version"]

    files = sorted(glob.glob(os.path.join(SALES_ROOT, "**", "*.parquet"), recursive=True))
    last = pd.Timestamp(os.path.basename(files[-1])[len("sales_"):-len(".parquet")]).date()
    first = last - timedelta(days=2 * WINDOW_DAYS - 1)
    daily = store_days(first - timedelta(days=HISTORY_DAYS), last)
    print(f"Backtesting {model} v{version} over {first}..{last}")

    step = first
    while step <= last:
        horizon = min(STEP_DAYS, (last - step).days + 1)
        history_start = step - timedelta(days=HISTORY_DAYS)
        for store, rows in daily.groupby("store_id"):
            window = rows[(rows["date"] >= history_start) & (rows["date"] < step)]
            history = [
                {
                    "date": row.date.isoformat(),
                    "sales": max(0.0, float(row.sales)),
                    "quantity_sold": max(0.0, float(row.quantity_sold)),
                    "profit": float(row.profit),
                    "has_promotion": bool(row.has_promotion),
                }
                for row in window.itertuples()
            ]
            call(
                "/v1/forecast",
                {
                    "store_id": store,
                    "model": model,
                    "model_version": version,
                    "horizon_days": horizon,
                    "history": history,
                },
                token,
            )
        print(f"  forecast {step} +{horizon}d for {daily['store_id'].nunique()} stores")
        step += timedelta(days=STEP_DAYS)

    # The trend charts plot one point per evaluation date, and an evaluation
    # without a reference date is anchored at the latest actual -- so replay
    # one per day across the scored window, ending on the latest actual.
    day = first + timedelta(days=WINDOW_DAYS - 1)
    while day < last:
        call(
            "/v1/monitoring/evaluate",
            {"model_version": version, "reference_date": day.isoformat(), "skip_actuals": True},
            token,
        )
        day += timedelta(days=1)

    result = call("/v1/monitoring/evaluate", {"model_version": version}, token)
    print(json.dumps({k: result[k] for k in (
        "status", "scored_pairs", "metrics_written", "drift_evaluated",
        "drift_detected", "drift_skipped_reason", "alert_titles", "recommendation",
    )}, indent=1))


if __name__ == "__main__":
    main()
