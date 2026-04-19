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
    
    with tab1:
        st.markdown("### Upload Historical Sales Data")
        uploaded_file = st.file_uploader(
            "Choose a CSV file",
            type=['csv'],
            help="File should contain: date, sales, and optionally store_id"
        )
        
        if uploaded_file is not None:
            input_data = pd.read_csv(uploaded_file)
            st.session_state.input_data = input_data
            st.success(f"✅ Loaded {len(input_data)} records")
            
            # Show preview
            with st.expander("Data Preview"):
                st.dataframe(input_data.head())
                
            # Basic validation
            required_cols = ['date', 'sales']
            missing_cols = [col for col in required_cols if col not in input_data.columns]
            if missing_cols:
                st.error(f"Missing required columns: {missing_cols}")
                input_data = None
                st.session_state.input_data = None
    
    with tab2:
        st.markdown("### Enter Recent Sales Data")
        
        col1, col2 = st.columns(2)
        with col1:
            store_id = st.text_input("Store ID", value="store_001")
        with col2:
            st.info("Enter sales for the last 7 days")
        
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
                    value=5000 + i*100,
                    key=f"manual_{i}",
                    label_visibility="collapsed"
                )
                manual_data.append({
                    'date': date,
                    'store_id': store_id,
                    'sales': sales
                })
        
        if st.button("Use Manual Data", key="manual_btn"):
            input_data = pd.DataFrame(manual_data)
            st.session_state.input_data = input_data
            st.success("✅ Manual data ready for prediction")
    
    with tab3:
        st.markdown("### Generate Sample Data")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            sample_days = st.number_input("Historical Days", value=60, min_value=7)
        with col2:
            avg_sales = st.number_input("Average Daily Sales", value=5000, min_value=100)
        with col3:
            volatility = st.slider("Volatility (%)", 0, 50, 20)
        
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
            st.session_state.input_data = input_data
            
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
                        
                    # Show metrics
                    st.markdown("### 📈 Forecast Summary")
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric(
                            "Total Forecast",
                            f"${results['summary']['total_predicted_sales']:,.0f}"
                        )
                    with col2:
                        st.metric(
                            "Daily Average",
                            f"${results['summary']['average_daily_sales']:,.0f}"
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
                        
                    predictions_df = results['predictions']
                    historical_mask = predictions_df.index < len(input_data)
                        
                    fig = go.Figure()

                    # Historical data
                    fig.add_trace(go.Scatter(
                        x=predictions_df[historical_mask]['date'],
                        y=input_data['sales'],
                        mode='lines',
                        name='Historical',
                        line=dict(color='blue', width=2)
                    ))

                    # Forecast
                    fig.add_trace(go.Scatter(
                        x=predictions_df[~historical_mask]['date'],
                        y=predictions_df[~historical_mask]['predicted_sales'],
                        mode='lines',
                        name='Forecast',
                        line=dict(color='green', width=3)
                    ))

                    # Confidence interval
                    fig.add_trace(go.Scatter(
                        x=predictions_df[~historical_mask]['date'],
                        y=predictions_df[~historical_mask]['upper_bound'],
                        fill=None,
                        mode='lines',
                        line_color='rgba(0,255,0,0)',
                        showlegend=False
                    ))

                    fig.add_trace(go.Scatter(
                        x=predictions_df[~historical_mask]['date'],
                        y=predictions_df[~historical_mask]['lower_bound'],
                        fill='tonexty',
                        mode='lines',
                        line_color='rgba(0,255,0,0.2)',
                        name='95% Confidence'
                    ))

                    fig.update_layout(
                        title="Sales Forecast with Confidence Intervals",
                        xaxis_title="Date",
                        yaxis_title="Sales ($)",
                        hovermode='x unified',
                        height=500,
                        showlegend=True
                    )
                        
                    st.plotly_chart(fig, use_container_width=True)
                        
                    st.markdown("### 💾 Export Results")

                    col1, col2 = st.columns(2)
                    with col1:
                        # Prepare download data
                        export_df = predictions_df[~historical_mask].copy()
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