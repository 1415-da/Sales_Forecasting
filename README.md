# Astro Sales Forecasting MLOps Platform

## Overview

A production-ready MLOps platform for sales forecasting that demonstrates modern machine learning engineering practices. Built on Astronomer (Apache Airflow), this project implements an end-to-end ML pipeline with ensemble modeling, comprehensive visualization, and real-time inference capabilities via Streamlit.

### 🚀 Key Features

- **Scenario Planner**: Predictive what-if comparisons against the baseline forecast (FastAPI + Next.js)
- **Automated ML Pipeline**: End-to-end orchestration with Astronomer/Airflow
- **Ensemble Modeling**: Combines XGBoost, LightGBM, and Prophet for robust predictions
- **Advanced Visualizations**: Comprehensive model performance analysis and comparison
- **Real-time Inference**: Streamlit-based web UI for interactive predictions
- **Experiment Tracking**: MLflow integration for model versioning and metrics
- **Distributed Storage**: MinIO S3-compatible object storage for artifacts
- **Containerized Deployment**: Docker-based architecture for consistency

## 🏗️ Architecture

### Technology Stack

| Component | Technology                 | Purpose |
|-----------|----------------------------|---------|  
| **Orchestration** | Astronomer (Airflow 3.0+)  | Workflow automation and scheduling |
| **ML Tracking** | MLflow 2.9+                | Experiment tracking and model registry |
| **Storage** | MinIO                      | S3-compatible artifact storage |
| **ML Models** | XGBoost, LightGBM, Prophet | Ensemble forecasting |
| **Visualization** | Matplotlib, Seaborn, Plotly | Model analysis and insights |
| **Inference UI** | Streamlit                  | Interactive prediction interface |
| **Serving API** | FastAPI                    | Scenario Planner endpoints, health, metrics |
| **Scenario UI** | Next.js + Tailwind + shadcn/ui | Scenario Planner frontend |
| **Containerization** | Docker & Docker Compose    | Environment consistency |

### System architecture

```
┌─ DATA ────────────────────────────────────────────────────────────────────┐
│  Kaggle M5  ·  local CSV  ·  seeded synthetic                             │
│  → data/raw/{source}/{version}/  +  dataset_manifest.json (sha256)        │
└────────────────┬──────────────────────────────────────────────────────────┘
                 ▼
┌─ ORCHESTRATION ─ Airflow / Astronomer ───────────────────────────────────┐
│  extract → validate → train → evaluate → register → promote → report     │
└────────────────┬──────────────────────────────────────────────────────────┘
                 ▼
┌─ TRACKING ───────────────────────────────────────────────────────────────┐
│  MLflow (:5001) ──backend──► PostgreSQL     ──artifacts──► MinIO (:9000) │
│  runs · params (dataset lineage) · metrics · model versions              │
└────────┬───────────────────────────────────┬─────────────────────────────┘
         │                                   │
         ▼                                   ▼
┌─ Streamlit (:8501) ──────────┐   ┌─ FastAPI (:8000) ─────────────────────┐
│  Direct inference UI         │   │  /v1/scenario/*  ·  /health  /metrics │
└──────────────────────────────┘   │  validation · capability · baseline   │
                                   │  overrides · impact · audit · rollback│
                                   └────────┬──────────────────────────────┘
                                            ▼
                                   ┌─ Next.js (:3000) ─────────────────────┐
                                   │  Scenario Planner UI                  │
                                   │  setup column + results panel         │
                                   └───────────────────────────────────────┘
```

## 🔮 Scenario Planner

Ask "what if we ran a 7-day promotion?" and get a comparison against the current
baseline forecast — per day, in total, with uncertainty and full lineage.

> **Scenarios are predictive what-if analyses, not causal guarantees.** A
> scenario runs the *same* model twice: once on the baseline forecast-time
> features and once with your assumptions applied. The difference is what the
> model **associates** with those conditions, based on historical patterns. It
> does not prove the scenario *causes* the change, and the intervals reflect
> model error only — not competitor action, supply shocks, or anything outside
> the training data. Every API response carries these limitations explicitly.

**Scenario types**: promotion · traffic change · store closure · holiday impact ·
price change · custom feature override.

Each is gated on what the selected model was actually trained on: if a model has
no `customer_traffic` feature, the traffic scenario is reported unsupported with
the missing features named, rather than silently returning a meaningless zero.

### Example request

```bash
curl -X POST http://localhost:8000/v1/scenario/compare \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: req_demo_001' \
  -H 'Idempotency-Key: 8f2c4b1e-7a3d-4c5e-9f10-2b6d8e4a1c73' \
  -d '{
    "store_id": "CA_1",
    "model_version": "latest",
    "horizon_days": 14,
    "confidence_levels": [0.1, 0.5, 0.9],
    "history": [
      {"date": "2026-01-01", "sales": 512.0},
      {"date": "2026-01-02", "sales": 498.0},
      {"date": "2026-01-03", "sales": 530.0},
      {"date": "2026-01-04", "sales": 547.0},
      {"date": "2026-01-05", "sales": 505.0},
      {"date": "2026-01-06", "sales": 489.0},
      {"date": "2026-01-07", "sales": 521.0}
    ],
    "scenario": {
      "scenario_type": "promotion",
      "start_date": "2026-01-08",
      "end_date": "2026-01-14",
      "promotion_intensity": "medium",
      "discount_depth": 0.25
    }
  }'
```

### Example response (abridged)

```json
{
  "scenario_id": "scn_4f8a2c1b9e7d4a3f8c2b1e9d7a4f3c8b",
  "baseline_forecast_id": "bf_9c3e7a1d5b8f2e6c4a9d3b7f1e5c8a2d",
  "scenario_forecast_id": "sf_2b6d8e4a1c73f9a5e3c7b1d9f4a8e2c6",
  "model_version": "14",
  "model_stage": "Production",
  "scenario_type": "promotion",
  "forecast_type": "predictive_what_if",
  "baseline": { "horizon_days": 14, "total_forecast": 7210.0, "has_quantiles": true },
  "scenario": { "horizon_days": 14, "total_forecast": 7528.0, "has_quantiles": true },
  "impact": {
    "horizon_days": 14,
    "total_baseline_forecast": 7210.0,
    "total_scenario_forecast": 7528.0,
    "total_difference": 318.0,
    "total_difference_pct": 4.41,
    "positive_days": 7,
    "negative_days": 0,
    "peak_impact_date": "2026-01-11",
    "peak_impact_value": 62.0,
    "impact_range": {
      "total_difference_p10": 180.0,
      "total_difference_p50": 318.0,
      "total_difference_p90": 455.0
    }
  },
  "applied_overrides": [
    { "feature_name": "is_promotion", "override_type": "set_constant", "affected_date_count": 7 }
  ],
  "unsupported_overrides": [],
  "modified_features": ["is_promotion", "discount_depth"],
  "dataset_lineage": {
    "data_source": "kaggle_m5",
    "dataset_version": "v1.0.0",
    "dataset_hash": "9f2c…",
    "lineage_status": "partial"
  },
  "limitations": [
    "This is a predictive what-if estimate based on the selected production model.",
    "It does not establish causal impact.",
    "Assumes scenario effects resemble historical patterns.",
    "Uncertainty reflects model error and does not include external shocks."
  ]
}
```

Full reference: **[docs/SCENARIO_PLANNER.md](docs/SCENARIO_PLANNER.md)** —
workflow, API contract, all error codes, rollback semantics, data lineage,
limitations, and the causal-upgrade path.

### Current status

`POST /v1/scenario/compare` returns `PREDICTION_FAILED` until two adapters are
implemented (`FutureFeatureBuilder`, `FeatureMatrixPredictor`) and training
publishes `feature_schema.json`. Capability discovery, validation, audit,
rollback, health, and metrics are complete and tested. See
[Limitations](docs/SCENARIO_PLANNER.md#limitations).

## 🚀 Quick Start

### Prerequisites

- Docker Desktop installed and running
- Astronomer CLI (`brew install astro` on macOS, other OS, you can follow the [instructions here](https://www.astronomer.io/docs/astro/cli/install-cli/))
- 8GB+ RAM available for Docker
- Ports 8080, 8501, 8000, 3000, 5001, 9000, 9001 available

### 1. Clone and Setup

```bash
# Clone the repository
git clone https://github.com/1415-da/Sales_Forecasting
cd Sales_Forecasting
```

### 2. Start All Services

```bash
# Start Astronomer Airflow services
astro dev start
```

This will start:

| Service | URL | Notes |
|---|---|---|
| Airflow UI | http://localhost:8080 | admin / admin |
| Scenario API | http://localhost:8000/docs | OpenAPI browser |
| API health | http://localhost:8000/health | api · db · MLflow · MinIO · feature schema |
| API metrics | http://localhost:8000/metrics | Prometheus text format |
| Scenario Planner UI | http://localhost:3000/scenario-planner | `--profile frontend` |
| Streamlit UI | http://localhost:8501 | direct inference |
| MLflow UI | http://localhost:5001 | experiments and registry |
| MinIO Console | http://localhost:9001 | minioadmin / minioadmin |

### 3. Run the ML Pipeline

1. Open Airflow UI at http://localhost:8080
2. Enable the `sales_forecast_training` DAG
3. Trigger the DAG manually or wait for scheduled run
4. Monitor progress in the Airflow UI

### 4. Use the Inference UI

1. Open Streamlit at http://localhost:8501
2. Click "Load/Reload Models" in the sidebar
3. Choose input method (upload CSV, manual entry, or sample data)
4. Configure forecast parameters
5. Generate predictions and export results

## 📊 ML Pipeline Features

### Data Processing
- **Pluggable, versioned training data** — default **`kaggle_m5`** via [kagglehub](https://github.com/Kaggle/kagglehub) (`m5-forecasting-accuracy`); see `data/README.md`
- **Deterministic synthetic benchmark** (`random_seed`, `dataset_version`, content hash → MLflow)
- Cached parquet under `data/raw/{source}/{version}/` (no delete-on-cleanup)
- Time-based train/validation/test splitting
- Comprehensive data validation and quality checks
- Advanced feature engineering (lags, rolling stats, seasonality)

### Model Training
- **XGBoost**: Gradient boosting for non-linear patterns
- **LightGBM**: Fast training with categorical support
- **Ensemble**: Optimized weighted average of all models
- Hyperparameter tuning with Optuna

### Visualization Suite
- Model performance comparison charts
- Time series predictions with confidence intervals
- Residual analysis and diagnostics
- Feature importance rankings
- Interactive plots with Plotly

### Model Management
- Automated experiment tracking with MLflow
- Model versioning and registry
- Artifact storage in MinIO
- Production model promotion workflow

## 🎯 Inference System

### Streamlit Features
- **Multiple Input Methods**: CSV upload, manual entry, sample data
- **Model Selection**: Individual models or ensemble
- **Interactive Visualizations**: Real-time prediction plots
- **Confidence Intervals**: 95% prediction bounds
- **Export Capabilities**: Download predictions as CSV

### API Architecture
```python
# Simplified prediction flow
Input Data → Feature Engineering → Model Prediction → Visualization → Export
```

## 📈 Performance & Metrics

- **Training Time**: ~2-5 minutes for full pipeline
- **Prediction Latency**: <100ms per forecast
- **Model Accuracy**: MAPE < 5% on test data
- **Ensemble Performance**: 15-20% improvement over individual models


## 🐛 Troubleshooting

### Common Issues

1. **Services not starting**: Check Docker memory allocation (8GB minimum)
2. **Models not loading**: Ensure training DAG has completed successfully
3. **Port conflicts**: Stop conflicting services or modify ports in docker-compose
4. **MLflow connection**: Verify MLflow service is running and accessible

### Logs and Debugging

```bash
# Check Airflow logs
astro dev logs

# Check specific service logs
docker-compose -f docker-compose.override.yml logs mlflow
docker-compose -f docker-compose.override.yml logs streamlit
```

## 📚 Documentation

- [Scenario Planner](docs/SCENARIO_PLANNER.md) — workflow, API contract, error codes, limitations
- [Training data sources](data/README.md) — pluggable providers and dataset versioning
- [Astronomer Docs](https://www.astronomer.io/docs/)

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
