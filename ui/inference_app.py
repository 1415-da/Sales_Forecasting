"""
ML Operations Console.

An internal operator view over the forecast API: what is serving, whether the
platform's dependencies are healthy, and a way to run a forecast end to end to
confirm the serving path works.

This app loads no model. It holds no MLflow client, no MinIO client, and no
object-store credentials; every number on screen came from an HTTP call to the
API. That is deliberate -- when model loading lived here, the console and the
product could drift apart and give different answers from the same registry.

The customer-facing experience is the Next.js Forecast Console. This page is for
operators, so it shows the things that view deliberately hides: dependency
probes, request ids, and raw response bodies.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from utils.api_client import (  # noqa: E402  (path setup must precede the import)
    ApiResult,
    api_base_url,
    create_forecast,
    get_health,
    get_models,
    login,
)

st.set_page_config(page_title="ML Operations Console", page_icon="🛠️", layout="wide")

st.markdown(
    """
    <style>
    .main .block-container {max-width: 1280px; padding-top: 1.5rem;}
    h1, h2, h3, h4, h5, h6, p, .stMarkdown {text-align: left !important;}
    </style>
    """,
    unsafe_allow_html=True,
)

HORIZON_OPTIONS = [7, 14, 30, 60, 90]
CONFIDENCE_OPTIONS = [0.95, 0.8]
MIN_HISTORY_DAYS = 30

# Metrics the training runs record, in the order an operator reads them.
METRIC_LABELS = {
    "rmse": "RMSE",
    "mae": "MAE",
    "r2": "R²",
    "wape": "WAPE",
    "mape": "MAPE",
    "bias": "Bias",
}
METRIC_COLORS = {
    "rmse": "#2563eb",
    "mae": "#7c3aed",
    "r2": "#16a34a",
    "wape": "#0891b2",
    "mape": "#ea580c",
    "bias": "#64748b",
}

if "history_df" not in st.session_state:
    st.session_state.history_df = None
if "history_source" not in st.session_state:
    st.session_state.history_source = None
if "forecast" not in st.session_state:
    st.session_state.forecast = None
# Per browser session: one Streamlit process serves every operator.
if "access_token" not in st.session_state:
    st.session_state.access_token = None
    st.session_state.signed_in_as = None


# ---------------------------------------------------------------------------
# Shared rendering
# ---------------------------------------------------------------------------


def render_api_failure(result: ApiResult, context: str) -> None:
    """
    Explain a failed call, with the request id an operator can grep for.

    The console's main job when something is wrong is to say *what* is wrong, so
    this never collapses a failure into "an error occurred".
    """
    st.error(f"{context}: {result.summary()}")
    columns = st.columns(3)
    columns[0].caption(f"API: {api_base_url()}")
    columns[1].caption(f"HTTP status: {result.status_code or 'no response'}")
    columns[2].caption(f"Request ID: {result.request_id or 'n/a'}")
    if result.details:
        with st.expander("Error details"):
            st.json(result.details)
    if result.error_code in ("API_UNREACHABLE", "API_TIMEOUT"):
        st.info(
            "The console reaches models only through the API. Start it with "
            "`docker compose up api`, then reload this page."
        )


def require_session(result: ApiResult) -> None:
    """
    Drop an expired or revoked token so the sidebar asks for a fresh sign-in.

    The console does not use the refresh cookie, so a 401 after sign-in means
    the access token has run out.
    """
    if result.status_code == 401 and st.session_state.access_token:
        st.session_state.access_token = None
        st.session_state.signed_in_as = None
        st.warning("Your session expired. Sign in again from the sidebar.")
        st.stop()


def render_sign_in() -> None:
    """Sidebar sign-in. Every /v1 route requires a token; /health does not."""
    with st.sidebar:
        st.subheader("Account")
        if st.session_state.access_token:
            st.caption(f"Signed in as {st.session_state.signed_in_as}")
            if st.button("Sign out", use_container_width=True):
                st.session_state.access_token = None
                st.session_state.signed_in_as = None
                st.session_state.forecast = None
                st.rerun()
            return

        with st.form("sign_in"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", use_container_width=True)
        if submitted:
            result = login(email.strip(), password)
            if result.ok:
                user = result.payload.get("user") or {}
                st.session_state.access_token = result.payload.get("access_token")
                st.session_state.signed_in_as = (
                    f"{user.get('email', email)} ({user.get('role', '?')})"
                )
                st.rerun()
            else:
                st.error(result.summary())
        st.caption("Use the same account as the web app.")


def build_sample_history(days: int, average: float, volatility_pct: int) -> pd.DataFrame:
    """
    Synthetic history with a weekly cycle and a mild trend.

    Seeded, so a sample forecast an operator runs twice gives the same answer
    and a real change in behaviour is attributable to the API, not the input.
    """
    generator = np.random.default_rng(20261021)
    dates = pd.date_range(end=datetime.now().date() - timedelta(days=1), periods=days, freq="D")
    trend = np.linspace(0, average * 0.06, days)
    seasonal = average * 0.08 * np.sin(2 * np.pi * np.arange(days) / 7)
    noise = generator.normal(0, average * volatility_pct / 100.0, days)
    sales = np.maximum(average + trend + seasonal + noise, 0.0)
    return pd.DataFrame({"date": dates, "sales": np.round(sales, 1)})


def history_payload(frame: pd.DataFrame, store_id: str) -> List[Dict[str, Any]]:
    """
    Convert a history frame into the API's request shape.

    Optional columns are forwarded only when present: sending a reconstructed
    value would stop the API from reporting it as reconstructed.
    """
    optional = ["quantity_sold", "profit", "customer_traffic"]
    ordered = frame.sort_values("date")
    payload: List[Dict[str, Any]] = []
    for _, row in ordered.iterrows():
        point: Dict[str, Any] = {
            "date": pd.to_datetime(row["date"]).strftime("%Y-%m-%d"),
            "sales": float(row["sales"]),
            "store_id": store_id,
        }
        for column in optional:
            if column in ordered.columns and pd.notna(row.get(column)):
                point[column] = float(row[column])
        payload.append(point)
    return payload


# ---------------------------------------------------------------------------
# Header and platform status
# ---------------------------------------------------------------------------

st.title("ML Operations Console")
st.caption(
    "Operator view over the forecast API. No model is loaded in this process; "
    "everything below comes from the API."
)

render_sign_in()

health = get_health()

if not health.ok:
    render_api_failure(health, "Platform health unavailable")
    st.stop()

health_data = health.payload
components = health_data.get("components", {})
production_model = health_data.get("production_model")

status_columns = st.columns(4)
status_columns[0].metric("API", health_data.get("status", "unknown"))
status_columns[1].metric("Version", health_data.get("version", "—"))
status_columns[2].metric(
    "Production model",
    f"{production_model['model']} v{production_model['version']}"
    if production_model
    else "none",
)
status_columns[3].metric(
    "Dependencies OK",
    f"{sum(1 for item in components.values() if item.get('status') == 'ok')}"
    f"/{len(components)}",
)

with st.expander("Dependency probes", expanded=not all(
    item.get("status") == "ok" for item in components.values()
)):
    if components:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "component": name,
                        "status": item.get("status"),
                        "latency_ms": item.get("latency_ms"),
                        "detail": item.get("detail", ""),
                    }
                    for name, item in components.items()
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No dependency detail was returned.")

st.markdown("---")

# ---------------------------------------------------------------------------
# Registered models
# ---------------------------------------------------------------------------

st.header("Registered models")

if not st.session_state.access_token:
    st.info("Sign in from the sidebar to see registered models and run a forecast.")
    st.stop()

models_result = get_models(token=st.session_state.access_token)
require_session(models_result)

if not models_result.ok:
    render_api_failure(models_result, "Model registry unavailable")
    st.stop()

models_data = models_result.payload
models: List[Dict[str, Any]] = models_data.get("models", [])

for warning in models_data.get("warnings", []):
    st.warning(warning)

if not models:
    st.info(
        "The registry holds no model versions. Trigger the "
        "`sales_forecast_training` DAG in Airflow to train, register, and "
        "promote a model."
    )
    st.stop()

st.dataframe(
    pd.DataFrame(
        [
            {
                "model": item["model"],
                "version": item["version"],
                "stage": item["stage"],
                "production": item["is_production"],
                "run_id": (item.get("run_id") or "")[:12],
                "trained": (item.get("training_date") or "")[:19],
                "features": item.get("feature_count"),
                "stores": len(item.get("supported_stores") or []),
                **{
                    label: item["metrics"].get(key)
                    for key, label in METRIC_LABELS.items()
                },
            }
            for item in models
        ]
    ),
    use_container_width=True,
    hide_index=True,
)

# Regression metrics, not a confusion matrix: this platform forecasts a
# continuous sales value, so a classification view would not apply.
metrics_figure = go.Figure()
for key, label in METRIC_LABELS.items():
    names = [item["model"] for item in models if item["metrics"].get(key) is not None]
    values = [
        item["metrics"][key] for item in models if item["metrics"].get(key) is not None
    ]
    if values:
        metrics_figure.add_trace(
            go.Bar(
                name=label,
                x=names,
                y=values,
                marker_color=METRIC_COLORS[key],
                text=[f"{value:.3f}" for value in values],
                textposition="auto",
            )
        )

if metrics_figure.data:
    metrics_figure.update_layout(
        title="Training metrics by model (lower is better except R²)",
        xaxis_title="Model",
        yaxis_title="Value",
        barmode="group",
        height=400,
        legend_title="Metric",
    )
    st.plotly_chart(metrics_figure, use_container_width=True)

st.markdown("---")

# ---------------------------------------------------------------------------
# Forecast test
# ---------------------------------------------------------------------------

st.header("Forecast test")
st.caption(
    "Exercises the full serving path: registry lookup, artifact load, feature "
    "construction, and recursive inference."
)

model_names = [item["model"] for item in models]
default_index = next(
    (index for index, item in enumerate(models) if item.get("is_default")), 0
)

control_columns = st.columns(4)
with control_columns[0]:
    selected_model = st.selectbox("Model", model_names, index=default_index)
selected = next(item for item in models if item["model"] == selected_model)
store_options = selected.get("supported_stores") or models_data.get("supported_stores", [])
with control_columns[1]:
    selected_store = st.selectbox(
        "Store", store_options or ["(none available)"], index=0
    )
with control_columns[2]:
    horizon = st.selectbox("Horizon (days)", HORIZON_OPTIONS, index=2)
with control_columns[3]:
    confidence = st.selectbox(
        "Confidence", CONFIDENCE_OPTIONS, index=0, format_func=lambda value: f"{value:.0%}"
    )

sample_tab, upload_tab = st.tabs(["Sample history", "Upload CSV"])

with sample_tab:
    sample_columns = st.columns(3)
    with sample_columns[0]:
        sample_days = st.number_input(
            "History days", min_value=MIN_HISTORY_DAYS, max_value=365, value=60
        )
    with sample_columns[1]:
        sample_average = st.number_input(
            "Average daily sales", min_value=1.0, value=3000.0, step=100.0
        )
    with sample_columns[2]:
        sample_volatility = st.slider("Volatility (%)", 0, 50, 5)

    if st.button("Load sample history", use_container_width=True):
        st.session_state.history_df = build_sample_history(
            int(sample_days), float(sample_average), int(sample_volatility)
        )
        st.session_state.history_source = "Sample history"
        st.session_state.forecast = None

with upload_tab:
    st.caption(
        "Needs a `date` column and a `sales` column. `quantity_sold`, `profit`, "
        "and `customer_traffic` are forwarded when present."
    )
    uploaded = st.file_uploader("CSV file", type=["csv"])
    if uploaded is not None:
        frame = pd.read_csv(uploaded)
        missing = [column for column in ("date", "sales") if column not in frame.columns]
        if missing:
            st.error(f"Missing required column(s): {', '.join(missing)}")
        else:
            frame["sales"] = pd.to_numeric(frame["sales"], errors="coerce")
            if frame["sales"].isna().any():
                st.error("The sales column contains values that are not numeric.")
            elif st.button("Use uploaded history", use_container_width=True):
                st.session_state.history_df = frame.dropna(subset=["sales"])
                st.session_state.history_source = f"Upload: {uploaded.name}"
                st.session_state.forecast = None

history_df: pd.DataFrame | None = st.session_state.history_df

if history_df is None:
    st.info("Load a history above to run a forecast.")
else:
    st.success(
        f"{len(history_df)} day(s) loaded — {st.session_state.history_source}"
    )
    if len(history_df) < MIN_HISTORY_DAYS:
        st.warning(
            f"The API requires at least {MIN_HISTORY_DAYS} days; this request "
            "will be rejected."
        )

    if st.button("Run forecast", type="primary", use_container_width=True):
        with st.spinner("Calling the forecast API…"):
            st.session_state.forecast = create_forecast(
                store_id=selected_store,
                model=selected_model,
                model_version="latest",
                horizon_days=int(horizon),
                confidence_level=float(confidence),
                history=history_payload(history_df, selected_store),
                token=st.session_state.access_token,
            )
        require_session(st.session_state.forecast)

forecast_result: ApiResult | None = st.session_state.forecast

if forecast_result is not None and not forecast_result.ok:
    render_api_failure(forecast_result, "Forecast failed")

if forecast_result is not None and forecast_result.ok:
    forecast = forecast_result.payload
    summary = forecast["summary"]
    metadata = forecast["metadata"]
    rows = pd.DataFrame(forecast["forecasts"])
    rows["date"] = pd.to_datetime(rows["date"])

    st.markdown("### Result")
    result_columns = st.columns(4)
    result_columns[0].metric("Total forecast", f"{summary['total_forecast_sales']:,.0f}")
    result_columns[1].metric("Daily average", f"{summary['average_daily_sales']:,.0f}")
    result_columns[2].metric(
        f"{forecast['confidence_level']:.0%} range",
        f"{summary['lower_bound_total']:,.0f} – {summary['upper_bound_total']:,.0f}",
    )
    result_columns[3].metric(
        "Served by",
        f"{forecast['model']} v{forecast['model_version']}",
        forecast["model_stage"],
    )

    figure = go.Figure()
    observed = history_df.sort_values("date") if history_df is not None else None
    if observed is not None:
        figure.add_trace(
            go.Scatter(
                x=pd.to_datetime(observed["date"]),
                y=observed["sales"],
                mode="lines",
                name="Actual",
                line={"color": "#94a3b8"},
            )
        )
    figure.add_trace(
        go.Scatter(
            x=rows["date"],
            y=rows["upper_bound"],
            mode="lines",
            name="Upper bound",
            line={"width": 0},
            showlegend=False,
        )
    )
    figure.add_trace(
        go.Scatter(
            x=rows["date"],
            y=rows["lower_bound"],
            mode="lines",
            name=f"{forecast['confidence_level']:.0%} interval",
            line={"width": 0},
            fill="tonexty",
            fillcolor="rgba(37, 99, 235, 0.18)",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=rows["date"],
            y=rows["predicted_sales"],
            mode="lines",
            name="Forecast",
            line={"color": "#2563eb", "dash": "dash"},
        )
    )
    figure.update_layout(
        title=f"Forecast for {forecast['store_id']}",
        xaxis_title="Date",
        yaxis_title="Sales",
        height=420,
    )
    st.plotly_chart(figure, use_container_width=True)

    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.download_button(
        "Download CSV",
        data=rows.to_csv(index=False).encode("utf-8"),
        file_name=(
            f"forecast_{forecast['store_id']}_{forecast['model']}"
            f"_v{forecast['model_version']}_{forecast['horizon_days']}d.csv"
        ),
        mime="text/csv",
    )

    # The API returns these caveats on every response. They change how the
    # numbers should be read, so the console shows them rather than filing them
    # under diagnostics.
    st.markdown("### How to read this forecast")
    for warning in forecast.get("warnings", []):
        st.caption(f"• {warning}")

    with st.expander("Diagnostics"):
        diagnostics_columns = st.columns(3)
        diagnostics_columns[0].caption(f"Forecast ID: {forecast['forecast_id']}")
        diagnostics_columns[1].caption(f"Request ID: {forecast_result.request_id}")
        diagnostics_columns[2].caption(f"Run ID: {metadata.get('run_id') or 'n/a'}")
        st.json(
            {
                "metadata": metadata,
                "summary": summary,
                "model_stage": forecast["model_stage"],
            }
        )

st.markdown("---")
st.caption(
    f"API: {api_base_url()} · The product-facing Forecast Console is the "
    "Next.js web app (http://localhost:3001 locally)."
)
