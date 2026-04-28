"""
Simplified Sales Forecasting Inference UI
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timedelta
import os
import sys

# Add paths
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from utils.simple_model_loader import SimpleModelLoader
from utils.simple_predictor import SimplePredictor
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Page config
st.set_page_config(
    page_title="Sales Forecast Inference",
    page_icon="🔮",
    layout="wide"
)

# Keep app content in a clean left-aligned flow.
st.markdown(
    """
    <style>
    .main .block-container {max-width: 1200px; padding-top: 1.5rem;}
    h1, h2, h3, h4, h5, h6, p, .stMarkdown {text-align: left !important;}
    </style>
    """,
    unsafe_allow_html=True,
)

# Initialize session state
if 'model_loader' not in st.session_state:
    st.session_state.model_loader = SimpleModelLoader()
    st.session_state.predictor = SimplePredictor(st.session_state.model_loader)
    st.session_state.models_loaded = False
    st.session_state.run_id = None
if 'input_data' not in st.session_state:
    st.session_state.input_data = None
if 'input_source' not in st.session_state:
    st.session_state.input_source = None

# Header
st.title("🔮 Sales Forecast Inference")
st.markdown("Generate sales predictions using trained ML models")

# Sidebar for model loading
with st.sidebar:
    st.header("📦 Model Configuration")
    
    if not st.session_state.models_loaded:
        st.warning("⚠️ No models loaded")
    else:
        st.success("✅ Models loaded")
        st.info(f"Models: {', '.join(st.session_state.model_loader.models.keys())}")
        if st.session_state.run_id:
            st.caption(f"Run ID: {st.session_state.run_id[:8]}...")
    
    if st.button("🔄 Load/Reload Models", type="primary", use_container_width=True):
        with st.spinner("Loading models..."):
            # Get latest run or use specific run
            run_id = st.session_state.model_loader.get_latest_run()
            if not run_id:
                # Use known good run ID as fallback
                run_id = "f4b632f644f742ceab8397bccac14da8"
                st.info(f"Using fallback run ID: {run_id[:8]}...")
            
            if run_id and st.session_state.model_loader.load_models_from_run(run_id):
                st.session_state.models_loaded = True
                st.session_state.run_id = run_id
                st.success("✅ Models loaded!")
                st.rerun()
            else:
                st.error("❌ Failed to load models")
    
    st.markdown("---")
    
    # Model selection
    model_type = st.selectbox(
        "Model Type",
        ["ensemble", "xgboost", "lightgbm"],
        help="Ensemble combines multiple models"
    )
    
    # Forecast settings
    forecast_days = st.slider(
        "Forecast Days",
        min_value=1,
        max_value=90,
        value=30
    )

# Main content
if st.session_state.models_loaded:
    # Input tabs
    tab1, tab2, tab3 = st.tabs(["📤 Upload Data", "✏️ Manual Entry", "🎲 Sample Data"])
    
    input_data = st.session_state.input_data
    if st.session_state.input_source:
        st.caption(f"Active input source: {st.session_state.input_source}")
    
    with tab1:
        st.markdown("### Upload Historical Sales Data")
        st.caption("Recommended scale for this demo: sales values in hundreds (for example, 120 to 900).")
        uploaded_file = st.file_uploader(
            "Choose a CSV file",
            type=['csv'],
            help="File should contain: date, sales, and optionally store_id"
        )
        
        if uploaded_file is not None:
            uploaded_df = pd.read_csv(uploaded_file)
            st.success(f"✅ Loaded {len(uploaded_df)} records")
            
            # Show preview
            with st.expander("Data Preview"):
                st.dataframe(uploaded_df.head())
                
            # Basic validation
            required_cols = ['date', 'sales']
            missing_cols = [col for col in required_cols if col not in uploaded_df.columns]
            if missing_cols:
                st.error(f"Missing required columns: {missing_cols}")
            elif pd.to_numeric(uploaded_df['sales'], errors='coerce').isna().any():
                st.error("Sales column contains non-numeric values. Please clean the file and re-upload.")
            else:
                uploaded_df['sales'] = pd.to_numeric(uploaded_df['sales'], errors='coerce')
                median_sales = float(uploaded_df['sales'].median())
                if median_sales > 1000:
                    st.warning(
                        "Uploaded sales appear to be in thousands scale. "
                        "For this frontend demo, convert to hundreds for more explainable outputs."
                    )
                    if st.button("Convert Uploaded Sales to Hundreds (/10)", key="uploaded_scale_hundreds_btn"):
                        uploaded_df['sales'] = uploaded_df['sales'] / 10.0
                        st.success("✅ Converted uploaded sales to hundreds scale")
                if st.button("Use Uploaded Data", key="uploaded_btn"):
                    st.session_state.input_data = uploaded_df
                    st.session_state.input_source = "Uploaded CSV"
                    st.success("✅ Uploaded data selected for prediction")
    
    with tab2:
        st.markdown("### Enter Recent Sales Data")
        
        col1, col2 = st.columns(2)
        with col1:
            store_id = st.text_input("Store ID", value="store_001")
        with col2:
            st.info("Enter sales for the last 7 days")

        include_all_manual_features = st.checkbox(
            "Include all training features (quantity_sold, profit, has_promotion, customer_traffic, is_holiday)",
            value=True,
            key="manual_include_all_features",
        )
        
        # Create input grid
        st.markdown("#### Daily Sales Input")
        cols = st.columns(7)
        manual_data = []
        
        for i in range(7):
            date = datetime.now() - timedelta(days=6-i)
            with cols[i]:
                st.caption(date.strftime('%a %m/%d'))
                sales = st.number_input(
                    "Sales ($)",
                    min_value=0,
                    value=420 + i * 15,
                    key=f"manual_{i}",
                    label_visibility="collapsed"
                )
                manual_data.append({
                    'date': date,
                    'store_id': store_id,
                    'sales': sales
                })

        if include_all_manual_features:
            st.markdown("#### Additional Feature Controls")
            f1, f2, f3, f4 = st.columns(4)
            with f1:
                quantity_multiplier = st.number_input(
                    "Quantity multiplier", min_value=0.05, max_value=2.0, value=0.21, step=0.01
                )
            with f2:
                profit_margin = st.number_input(
                    "Profit margin", min_value=0.01, max_value=0.90, value=0.30, step=0.01
                )
            with f3:
                traffic_multiplier = st.number_input(
                    "Traffic multiplier", min_value=0.10, max_value=5.0, value=0.95, step=0.05
                )
            with f4:
                promo_frequency = st.number_input(
                    "Promotion every N days", min_value=2, max_value=14, value=3, step=1
                )

            for idx, row in enumerate(manual_data):
                row["quantity_sold"] = int(max(1, round(row["sales"] * quantity_multiplier)))
                row["profit"] = round(row["sales"] * profit_margin, 2)
                row["customer_traffic"] = int(max(50, round(row["sales"] * traffic_multiplier)))
                row["has_promotion"] = 1 if idx % promo_frequency == 0 else 0
                row["is_holiday"] = 1 if row["date"].weekday() >= 5 else 0
        
        if st.button("Use Manual Data", key="manual_btn"):
            input_data = pd.DataFrame(manual_data)
            st.session_state.input_data = input_data
            st.session_state.input_source = "Manual Entry"
            st.success("✅ Manual data ready for prediction")
    
    with tab3:
        st.markdown("### Generate Sample Data")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            sample_days = st.number_input("Historical Days", value=60, min_value=7)
        with col2:
            avg_sales = st.number_input("Average Daily Sales", value=520, min_value=50)
        with col3:
            volatility = st.slider("Volatility (%)", 0, 50, 20)

        include_all_sample_features = st.checkbox(
            "Include all training features in generated sample data",
            value=True,
            key="sample_include_all_features",
        )
        s1, s2, s3, s4 = st.columns(4)
        with s1:
            sample_quantity_multiplier = st.number_input(
                "Sample quantity multiplier", min_value=0.05, max_value=2.0, value=0.21, step=0.01
            )
        with s2:
            sample_profit_margin = st.number_input(
                "Sample profit margin", min_value=0.01, max_value=0.90, value=0.30, step=0.01
            )
        with s3:
            sample_traffic_multiplier = st.number_input(
                "Sample traffic multiplier", min_value=0.10, max_value=5.0, value=0.95, step=0.05
            )
        with s4:
            sample_promo_every = st.number_input(
                "Sample promotion every N days", min_value=2, max_value=14, value=3, step=1
            )
        
        if st.button("Generate Sample Data", key="sample_btn"):
            # Generate realistic sample data
            dates = pd.date_range(end=datetime.now(), periods=sample_days, freq='D')
            
            # Add trend and seasonality
            trend = np.linspace(0, avg_sales * 0.1, sample_days)
            seasonal = avg_sales * 0.2 * np.sin(2 * np.pi * np.arange(sample_days) / 7)
            noise = np.random.normal(0, avg_sales * volatility / 100, sample_days)
            
            sales = avg_sales + trend + seasonal + noise
            sales = np.maximum(sales, 0)  # Ensure non-negative
            
            input_data = pd.DataFrame({
                'date': dates,
                'store_id': 'store_001',
                'sales': sales
            })
            if include_all_sample_features:
                input_data['quantity_sold'] = np.maximum(
                    (sales * sample_quantity_multiplier).round().astype(int), 1
                )
                input_data['profit'] = np.round(sales * sample_profit_margin, 2)
                input_data['has_promotion'] = (
                    (dates.dayofweek.isin([4, 5])) & (np.arange(sample_days) % sample_promo_every == 0)
                ).astype(int)
                input_data['customer_traffic'] = np.maximum(
                    (sales * sample_traffic_multiplier).round().astype(int), 50
                )
                input_data['is_holiday'] = dates.dayofweek.isin([5, 6]).astype(int)
            st.session_state.input_data = input_data
            st.session_state.input_source = "Sample Data"
            
            st.success("✅ Sample data generated")
            
            # Show chart
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=input_data['date'],
                y=input_data['sales'],
                mode='lines',
                name='Sample Sales Data'
            ))
            fig.update_layout(
                title="Generated Sample Data",
                xaxis_title="Date",
                yaxis_title="Sales ($)",
                height=300
            )
            st.plotly_chart(fig, use_container_width=True)
    
    # Prediction section
    input_data = st.session_state.input_data
    if input_data is not None:
        st.markdown("---")
        st.header("📊 Generate Forecast")
        st.caption(
            f"Current active dataset: {len(input_data)} rows, {len(input_data.columns)} columns"
        )
        with st.expander("🧾 Current active columns", expanded=False):
            st.write(", ".join([str(col) for col in input_data.columns.tolist()]))
            st.dataframe(input_data.head(5), use_container_width=True)

        if st.button("🚀 Run Prediction", type="primary", use_container_width=True, key="run_prediction"):
            with st.spinner("Generating forecast..."):
                # Run prediction
                results = st.session_state.predictor.predict(
                    input_data,
                    model_type=model_type,
                    forecast_days=forecast_days
                )
                    
                if results['success']:
                    st.success("✅ Forecast generated successfully!")

                    unit_suffix = " ($)"
                    unit_axis = "Sales ($)"
                        
                    # Show metrics
                    st.markdown("### 📈 Forecast Summary")
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric(
                            f"Total Forecast{unit_suffix}",
                            f"{results['summary']['total_predicted_sales']:,.0f}"
                        )
                    with col2:
                        st.metric(
                            f"Daily Average{unit_suffix}",
                            f"{results['summary']['average_daily_sales']:,.0f}"
                        )
                    with col3:
                        st.metric(
                            "Forecast Period",
                            f"{forecast_days} days"
                        )
                    with col4:
                        st.metric(
                            "Model Used",
                            model_type.upper()
                        )
                        
                    # Visualization
                    st.markdown("### 📊 Forecast Visualization")
                    st.info(
                        "This chart compares historical sales (blue) with forecasted sales (green). "
                        "The shaded band shows the 95% confidence range (lower to upper bound), "
                        "which indicates possible variation around the main forecast."
                    )
                        
                    predictions_df = results['predictions'].copy()
                    predictions_df["date"] = pd.to_datetime(predictions_df["date"])
                    historical_df = input_data.copy()
                    historical_df["date"] = pd.to_datetime(historical_df["date"])
                    forecast_only = predictions_df.copy()
                        
                    fig = go.Figure()
                    # Show only recent history so forecast and confidence band remain readable.
                    history_window_days = min(30, len(historical_df))
                    historical_display_df = historical_df.tail(history_window_days).copy()

                    # Historical data
                    fig.add_trace(go.Scatter(
                        x=historical_display_df['date'],
                        y=historical_display_df['sales'],
                        mode='lines',
                        name='Historical',
                        line=dict(color='blue', width=2)
                    ))

                    # Forecast
                    fig.add_trace(go.Scatter(
                        x=forecast_only['date'],
                        y=forecast_only['predicted_sales'],
                        mode='lines',
                        name='Forecast',
                        line=dict(color='green', width=3)
                    ))

                    # Confidence interval
                    fig.add_trace(go.Scatter(
                        x=forecast_only['date'],
                        y=forecast_only['upper_bound'],
                        fill=None,
                        mode='lines',
                        line=dict(color='rgba(34,139,34,0.35)', width=1, dash='dot'),
                        showlegend=False
                    ))

                    fig.add_trace(go.Scatter(
                        x=forecast_only['date'],
                        y=forecast_only['lower_bound'],
                        fill='tonexty',
                        mode='lines',
                        line=dict(color='rgba(34,139,34,0.35)', width=1, dash='dot'),
                        fillcolor='rgba(34,139,34,0.22)',
                        name='95% Confidence'
                    ))

                    # Dynamic y-range using visible history + forecast bounds
                    y_values = np.concatenate(
                        [
                            historical_display_df['sales'].astype(float).values,
                            forecast_only['lower_bound'].astype(float).values,
                            forecast_only['upper_bound'].astype(float).values,
                        ]
                    )
                    y_min = float(np.min(y_values))
                    y_max = float(np.max(y_values))
                    y_pad = max((y_max - y_min) * 0.08, 1.0)

                    fig.update_layout(
                        title="Sales Forecast with Confidence Intervals",
                        xaxis_title="Date",
                        yaxis_title=unit_axis,
                        hovermode='x unified',
                        height=500,
                        showlegend=True,
                        yaxis=dict(range=[y_min - y_pad, y_max + y_pad]),
                    )
                        
                    st.plotly_chart(fig, use_container_width=True)

                    # Additional forecasting explainability charts
                    forecast_only["cumulative_forecast"] = forecast_only["predicted_sales"].cumsum()
                    forecast_only["interval_width"] = (
                        forecast_only["upper_bound"] - forecast_only["lower_bound"]
                    )

                    st.markdown("### 📈 Cumulative Forecast")
                    st.caption(
                        "Running total of predicted sales across the forecast period. "
                        "Use this to compare expected revenue against targets."
                    )

                    cumulative_y = forecast_only["cumulative_forecast"].astype(float).values
                    cum_min = float(np.min(cumulative_y))
                    cum_max = float(np.max(cumulative_y))
                    cum_pad = max((cum_max - cum_min) * 0.12, 1.0)

                    cumulative_fig = go.Figure()
                    cumulative_fig.add_trace(
                        go.Scatter(
                            x=forecast_only["date"],
                            y=forecast_only["cumulative_forecast"],
                            mode="lines+markers",
                            name="Cumulative Forecast",
                            line=dict(color="#1f77b4", width=3),
                            marker=dict(size=5),
                        )
                    )
                    cumulative_fig.update_layout(
                        title="Running Total Predicted Sales",
                        xaxis_title="Date",
                        yaxis_title=f"Cumulative {unit_axis}",
                        hovermode="x unified",
                        height=380,
                        yaxis=dict(range=[cum_min - cum_pad, cum_max + cum_pad]),
                    )
                    st.plotly_chart(cumulative_fig, use_container_width=True)

                    st.markdown("### 📏 Prediction Interval Width")
                    st.caption(
                        "Shows forecast uncertainty over time as (upper bound - lower bound). "
                        "Higher values mean lower confidence for that date."
                    )
                    width_fig = go.Figure()
                    width_fig.add_trace(
                        go.Scatter(
                            x=forecast_only["date"],
                            y=forecast_only["interval_width"],
                            mode="lines+markers",
                            name="Uncertainty Width",
                            line=dict(color="#ff7f0e", width=2),
                        )
                    )
                    width_fig.update_layout(
                        title="Uncertainty Band Width Over Time",
                        xaxis_title="Date",
                        yaxis_title="Upper - Lower Bound ($)",
                        hovermode="x unified",
                        height=350,
                    )
                    st.plotly_chart(width_fig, use_container_width=True)

                    st.markdown("### 🗓️ Forecast Intensity Calendar Heatmap")
                    st.caption(
                        "Highlights busy vs slow forecast days. Darker cells indicate higher predicted sales, "
                        "helping with staffing, inventory, and campaign planning."
                    )
                    day_order = [
                        "Monday",
                        "Tuesday",
                        "Wednesday",
                        "Thursday",
                        "Friday",
                        "Saturday",
                        "Sunday",
                    ]
                    heatmap_df = forecast_only.copy()
                    heatmap_df["day_name"] = heatmap_df["date"].dt.day_name()
                    heatmap_df["day_name"] = pd.Categorical(
                        heatmap_df["day_name"], categories=day_order, ordered=True
                    )
                    heatmap_df["week_start"] = heatmap_df["date"] - pd.to_timedelta(
                        heatmap_df["date"].dt.weekday, unit="D"
                    )
                    heatmap_matrix = (
                        heatmap_df.pivot_table(
                            index="day_name",
                            columns="week_start",
                            values="predicted_sales",
                            aggfunc="mean",
                        )
                        .reindex(day_order)
                        .sort_index(axis=1)
                    )

                    heatmap_fig = go.Figure(
                        data=go.Heatmap(
                            z=heatmap_matrix.values,
                            x=[d.strftime("%Y-%m-%d") for d in heatmap_matrix.columns],
                            y=heatmap_matrix.index.tolist(),
                            colorscale="Blues",
                            colorbar=dict(title="Predicted<br>Sales ($)"),
                            hoverongaps=False,
                        )
                    )
                    heatmap_fig.update_layout(
                        title="Daily Forecast Intensity (Busy vs Slow Days)",
                        xaxis_title="Week Start",
                        yaxis_title="Day of Week",
                        height=420,
                    )
                    st.plotly_chart(heatmap_fig, use_container_width=True)
                        
                    st.markdown("### 💾 Export Results")

                    col1, col2 = st.columns(2)
                    with col1:
                        # Prepare download data
                        export_df = forecast_only.copy()
                        export_df = export_df.round(2)

                        csv = export_df.to_csv(index=False)
                        st.download_button(
                            label="📥 Download Forecast (CSV)",
                            data=csv,
                            file_name=f"sales_forecast_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                            mime="text/csv"
                        )

                    with col2:
                        st.info("Forecast includes predictions with confidence intervals")
                    
                else:
                    st.error(f"❌ Prediction failed: {results['error']}")

else:
    # No models loaded
    st.warning("⚠️ Please load models using the sidebar before making predictions.")
    st.info("👈 Click 'Load/Reload Models' in the sidebar to begin")
    
    # Add helpful information
    with st.expander("ℹ️ No models found? Here's what to do:", expanded=True):
        st.markdown("""
        ### First Time Setup
        
        If this is your first time using the system, you need to train the models:
        
        1. **Open Airflow UI**: [http://localhost:8080](http://localhost:8080)
        - Username: `admin`
        - Password: `admin`
        
        2. **Run the Training DAG**:
        - Find `sales_forecast_training` in the DAG list
        - Click the play button (▶️) to trigger it
        - Wait for training to complete (5-10 minutes)
        
        3. **Come back here**:
        - Click "Load/Reload Models" again
        - Models should load successfully
        
        ### Quick Check
        
        - **MLflow UI**: [http://localhost:5001](http://localhost:5001) - Check if models exist
        - **MinIO UI**: [http://localhost:9001](http://localhost:9001) - Check artifact storage
        - Username: `minioadmin`
        - Password: `minioadmin`
        """)