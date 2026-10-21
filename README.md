# Astro Sales Forecasting MLOps Platform

## Overview

An MLOps platform for sales forecasting, built on Astronomer (Apache Airflow).
It trains, registers, and promotes ensemble models, then serves forecasts
through a FastAPI layer to a Next.js web application — so a user can go from a
sales history to a forecast with a chart, a confidence band, and a CSV export
without touching Airflow, MLflow, MinIO, or a notebook.

### 🚀 Key Features

- **Forecast Console**: pick a store, horizon, model, and confidence level;
  supply demo, uploaded, or manually entered history; get a forecast with an
  uncertainty band, KPIs, a sortable table, and CSV export
- **Server-side inference only**: every model load and prediction happens in
  FastAPI; no client ever receives an MLflow, MinIO, or database credential
- **Scenario Planner**: predictive what-if comparisons against the baseline
- **Automated ML pipeline**: orchestration with Astronomer/Airflow
- **Ensemble modelling**: XGBoost, LightGBM, and a weighted ensemble, tuned
  with Optuna
- **Experiment tracking**: MLflow runs, dataset lineage, and a model registry
  backed by PostgreSQL with artifacts in MinIO
- **Honest reporting**: every forecast declares how its interval was built and
  which model inputs had to be reconstructed; see
  [the caveats](#how-the-forecast-is-built-and-what-it-can-and-cannot-tell-you)
- **Containerised**: one `astro dev start` brings up the whole stack

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

Every model load and every prediction happens inside FastAPI. Neither the
browser nor the Streamlit console holds an MLflow client, a MinIO client, or an
object-store credential.

```
┌─ DATA ────────────────────────────────────────────────────────────────────┐
│  Kaggle M5  ·  local CSV  ·  seeded synthetic                             │
│  → data/raw/{source}/{version}/  +  dataset_manifest.json (sha256)        │
└────────────────┬──────────────────────────────────────────────────────────┘
                 ▼
┌─ ORCHESTRATION ─ Airflow / Astronomer (:8080) ───────────────────────────┐
│  extract → validate → train → evaluate → register → promote → report     │
└────────────────┬──────────────────────────────────────────────────────────┘
                 ▼
┌─ TRACKING ───────────────────────────────────────────────────────────────┐
│  MLflow (:5001) ──backend──► PostgreSQL     ──artifacts──► MinIO (:9000) │
│  runs · params (dataset lineage) · metrics · registered model versions   │
└────────────────┬──────────────────────────────────────────────────────────┘
                 │  registry lookup + artifact download (server-side only)
                 ▼
┌─ SERVING ─ FastAPI (:8000) ──────────────────────────────────────────────┐
│  GET  /v1/models            which version serves each model              │
│  GET  /v1/models/{version}  metrics · features · dataset lineage         │
│  POST /v1/forecast          recursive multi-day forecast + interval      │
│  /v1/scenario/*             predictive what-if (Scenario Planner)        │
│  /health  /health/ready  /metrics                                        │
│                                                                           │
│  model_registry_service → artifact_service → feature_service             │
│                                   → forecast_service (one pipeline)      │
└───────┬───────────────────────────────────────────┬──────────────────────┘
        │ HTTP (JSON only)                          │ HTTP (JSON only)
        ▼                                           ▼
┌─ Next.js (:3000) ────────────────┐   ┌─ Streamlit (:8501) ───────────────┐
│  Forecast Console  (product)     │   │  ML Operations Console (internal) │
│  Scenario Planner                │   │  model status · dependency probes │
│  chart · KPIs · table · CSV      │   │  forecast test · request ids      │
└──────────────────────────────────┘   └───────────────────────────────────┘
```

## 📈 Forecast Console — the end-to-end workflow

The product-facing path. A user opens the web app, supplies a sales history,
and gets a forecast back without touching Airflow, MLflow, MinIO, a notebook,
or a model file.

1. Open **http://localhost:3000** → the Forecast Console.
2. The top bar shows API health and the current Production model; the Model
   selector lists what the registry is actually serving.
3. Pick a **store**, **horizon** (7 / 14 / 30 / 60 / 90), **model**
   (Ensemble / XGBoost / LightGBM), **version** (Latest Production or a
   specific one), and **confidence** (80% or 95%).
4. Supply **history** one of three ways: one-click **demo data** (seeded, and
   labelled as demo), a **CSV upload** (`date` + `sales` required), or
   **manual entry** from a dated template. The preview reports row count, date
   range, missing days, and anything that blocks the run.
5. Select **Generate Forecast**.
6. Read the result: KPI cards, a chart with the confidence band (daily or
   cumulative), a sortable day-by-day table, and **Download CSV**.

### `POST /v1/forecast`

```bash
curl -s http://localhost:8000/v1/forecast \
  -H 'Content-Type: application/json' \
  -d '{
    "store_id": "CA_1",
    "model": "ensemble",
    "model_version": "latest",
    "horizon_days": 7,
    "confidence_level": 0.95,
    "history": [
      {"date": "2026-08-22", "sales": 2980},
      {"date": "2026-08-23", "sales": 3120}
    ]
  }'
```

`history` must hold at least `FORECAST_MIN_HISTORY_DAYS` (default 30) days,
ordered oldest-first with no duplicate dates. Store ids come from
`GET /v1/models` — they are the ones the selected model's encoder recognises.

```json
{
  "forecast_id": "fc_9f2c1a7b4e6d8f03",
  "store_id": "CA_1",
  "model": "ensemble",
  "model_version": "1",
  "model_stage": "Production",
  "horizon_days": 7,
  "confidence_level": 0.95,
  "summary": {
    "total_forecast_sales": 21402.5,
    "average_daily_sales": 3057.5,
    "lower_bound_total": 19654.1,
    "upper_bound_total": 23150.9
  },
  "forecasts": [
    {"date": "2026-10-22", "predicted_sales": 3005.2,
     "lower_bound": 2813.0, "upper_bound": 3197.4}
  ],
  "metadata": {
    "history_days": 60,
    "feature_schema_version": "v1",
    "feature_count": 51,
    "uncertainty_method": "residual_normal",
    "residual_std": 98.08,
    "imputed_features": ["quantity_sold", "profit", "customer_traffic", "has_promotion"],
    "run_id": "67a0ca0922af4ea289f76a9a943ecafd",
    "data_source": "kaggle_m5",
    "dataset_version": "v1.0.0"
  },
  "warnings": ["..."]
}
```

### How the forecast is built, and what it can and cannot tell you

**Recursive projection.** Each day's lag and rolling inputs come from the
observed history extended by the days already predicted. Error therefore
compounds with horizon, and the interval widens by `sqrt(step)` to say so.

**Uncertainty is constructed, not native.** The trained models are point
estimators. `metadata.uncertainty_method` reports which method produced the
band: `residual_normal` (a normal interval around the point forecast using the
model's own held-out RMSE, the preferred path) or `history_volatility` (a
fallback approximated from the supplied history's volatility, which describes
the data rather than the model — and says so in `warnings`).

**Some model inputs cannot be known for a future date, and are reconstructed.**
In the M5-derived training frame `quantity_sold == sales` and
`profit == 0.3 × sales` hold exactly, because both are summed from the same
per-product quantity. For a forecast day they are rebuilt from the most recent
observed level; `customer_traffic` and `has_promotion` are carried from recent
history, or held at the value they had throughout training when history does
not supply them; `is_holiday` is reproduced from the same US public-holiday
calendar the training pipeline used. Every reconstructed input is listed in
`metadata.imputed_features`.

**The honest consequence.** Because `quantity_sold` is the training target
under another name, the model leans heavily on it, so a forecast driven by a
lag-1 proxy behaves largely as a **level projection** rather than an
independent prediction of day-to-day movement. The API states this in
`warnings` on every response and the UI shows it under *How to read this
forecast*. Removing the leakage from the feature set and retraining is the
single highest-value next change to this platform.

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

- Docker Desktop installed and running, 8 GB+ RAM allocated
- Astronomer CLI (`brew install astro` on macOS; see the
  [install guide](https://www.astronomer.io/docs/astro/cli/install-cli/))
- Free ports: 3000, 8000, 8080, 8501, 5001, 9000, 9001 — or override
  `API_PORT` / `FRONTEND_PORT` in `.env` (see below)

### 1. Clone and configure

```bash
git clone https://github.com/1415-da/Sales_Forecasting
cd Sales_Forecasting
cp .env.example .env     # optional: every value has a working default
```

#### If 3000 or 8000 are already taken

Four variables move together — the browser needs a URL it can reach, so
`NEXT_PUBLIC_API_URL` must match `API_PORT`, and the API must accept the
frontend's origin:

```bash
API_PORT=8010
FRONTEND_PORT=3100
NEXT_PUBLIC_API_URL=http://localhost:8010
API_CORS_ALLOW_ORIGINS=http://localhost:3100,http://127.0.0.1:3100
```

Pass them in the shell rather than putting them in `.env`:

```bash
API_PORT=8010 FRONTEND_PORT=3100 \
NEXT_PUBLIC_API_URL=http://localhost:8010 \
API_CORS_ALLOW_ORIGINS=http://localhost:3100,http://127.0.0.1:3100 \
astro dev start
```

Freeing the ports is simpler if you can. Check what holds them with
`lsof -nP -iTCP:8000 -sTCP:LISTEN`.

### 2. Start the stack

```bash
astro dev start
```

First start is slow, and all of it is one-off:

- the `api` image installs its ML stack (~250 MB of wheels) — Compose builds it
  automatically via `pull_policy: build`, or build it yourself first with
  `docker compose build api`;
- the `mlflow` and `streamlit-ui` containers `pip install` at boot (the MLflow
  healthcheck allows 7 minutes for this, the console 3);
- `frontend` runs `npm install` into a named volume.

Subsequent starts are fast. Downloaded model artifacts also persist, in the
`api-model-cache` volume.

| Service | URL | Notes |
|---|---|---|
| **Forecast Console** | http://localhost:3000 | the product UI |
| Scenario Planner | http://localhost:3000/scenario-planner | predictive what-if |
| **FastAPI docs** | http://localhost:8000/docs | OpenAPI browser |
| API health | http://localhost:8000/health | api · db · MLflow · MinIO · schema · model |
| API metrics | http://localhost:8000/metrics | Prometheus text format |
| ML Operations Console | http://localhost:8501 | internal Streamlit view |
| Airflow UI | http://localhost:8080 | admin / admin |
| MLflow UI | http://localhost:5001 | experiments and registry |
| MinIO Console | http://localhost:9001 | minioadmin / minioadmin |

### 3. Train and promote a model

```bash
# In the Airflow UI at http://localhost:8080
# 1. Enable the `sales_forecast_training` DAG
# 2. Trigger it
```

The DAG runs `extract → validate → train → evaluate → register → promote →
report → cleanup`. It registers `sales_forecast_models_{xgboost,lightgbm,
ensemble}` and promotes them to the **Production** stage. Until a Production
version exists, the Forecast Console says so and the Generate button stays
disabled — it never invents a forecast.

### 4. Generate a forecast

```bash
curl -s http://localhost:8000/v1/models | python3 -m json.tool
```

Then open **http://localhost:3000**, load the demo history, and select
**Generate Forecast**. See
[Forecast Console](#-forecast-console--the-end-to-end-workflow) for the full
walkthrough.

### Environment variables

Full list with comments in [`.env.example`](.env.example). The ones that matter
most:

| Variable | Default | Purpose |
|---|---|---|
| `API_PORT` | `8000` | host port for FastAPI |
| `FRONTEND_PORT` | `3000` | host port for Next.js |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | API URL the **browser** uses |
| `API_CORS_ALLOW_ORIGINS` | `http://localhost:3000,...` | browser origins allowed to call the API |
| `MLFLOW_REGISTERED_MODEL_PREFIX` | `sales_forecast_models_` | registry naming |
| `FORECAST_MODELS` | `ensemble,xgboost,lightgbm` | models offered |
| `FORECAST_DEFAULT_MODEL` | `ensemble` | preselected model |
| `FORECAST_REQUIRE_PRODUCTION_STAGE` | `true` | refuse to serve an unpromoted version |
| `FORECAST_MIN_HISTORY_DAYS` | `30` | minimum history accepted |
| `FORECAST_MAX_HORIZON_DAYS` | `90` | maximum horizon |
| `FORECAST_PROFIT_TO_SALES_RATIO` | `0.3` | ratio used to rebuild `profit` |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `minioadmin` | **local defaults — change anywhere real** |

Credentials reach the API and MLflow only. The frontend receives
`NEXT_PUBLIC_API_URL` and nothing else.

### Tests

```bash
# Backend: 574 tests (services, routes, error contract, scenario planner)
python3 -m pytest api/tests tests/api tests/test_dataset_lineage.py -q

# Frontend: 241 tests + strict typecheck
cd frontend && npm test && npm run typecheck
```

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

## 🎯 Serving layer

All inference lives in `api/`. One pipeline serves the Forecast Console, the
Streamlit console, and the Scenario Planner's baseline leg, so the same inputs
produce the same number everywhere.

```
POST /v1/forecast
  └─ model_registry_service   which version serves (registry, read-only)
     └─ artifact_service      download + cache model, scaler, encoder, features
        └─ feature_service    calendar · cyclical · lag · rolling · exogenous
           └─ forecast_service  recursive projection + uncertainty band
```

| Module | Responsibility |
|---|---|
| `api/config.py` | all environment reads, in one frozen `Settings` |
| `api/deps.py` | service lifetimes, so routes are overridable in tests |
| `api/services/model_registry_service.py` | registry lookups; metrics and lineage |
| `api/services/artifact_service.py` | artifact fetch (proxy transport) + LRU cache |
| `api/services/feature_service.py` | the canonical feature contract |
| `api/services/forecast_service.py` | recursion, uncertainty, model discovery |
| `api/routers/{forecast,models,health}.py` | thin HTTP layer |
| `api/core/errors.py` | one error taxonomy and one response envelope |

### Endpoints

| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | API, database, MLflow, MinIO, feature schema, Production model |
| `GET` | `/health/ready` | 503 when a critical dependency is down |
| `GET` | `/metrics` | Prometheus text exposition |
| `GET` | `/v1/models` | serving version per model, with metrics and stores |
| `GET` | `/v1/models/{version}` | one version: metrics, feature list, lineage |
| `POST` | `/v1/forecast` | multi-day forecast, summary, metadata, warnings |
| `*` | `/v1/scenario/*` | Scenario Planner (see above) |

Every failure uses one envelope:

```json
{
  "success": false,
  "error": {
    "code": "INSUFFICIENT_HISTORY",
    "message": "At least 30 days of history are required; 10 were supplied.",
    "field": "history",
    "details": {"required_days": 30, "supplied_days": 10},
    "request_id": "req_4f1c...",
    "timestamp": "2026-10-21T09:30:00Z"
  }
}
```

| Code | Status | Meaning |
|---|---|---|
| `INSUFFICIENT_HISTORY` | 422 | fewer days than the model's windows need |
| `SCENARIO_VALIDATION_ERROR` | 422 | a field is unusable (e.g. an unknown store) |
| `MODEL_NOT_FOUND` | 404 | unknown model or version |
| `MODEL_NOT_IN_PRODUCTION` | 409 | the version exists but is not promoted |
| `FEATURE_MISMATCH` | 409 | the model needs inputs a sales history cannot provide |
| `ARTIFACT_DOWNLOAD_FAILED` | 502 | the artifact could not be retrieved |
| `PREDICTION_FAILED` | 500 | the model failed during inference |
| `MLFLOW_UNAVAILABLE` | 503 | the registry could not be reached |

No stack trace, filesystem path, connection string, object-store key, or
internal hostname ever appears in a response: everything client-facing passes
through `sanitize_text` / `sanitize_details`. The `request_id` is echoed as
`X-Request-ID` and shown in both UIs so a user can quote one greppable
identifier.

### Web application (`frontend/`)

Next.js App Router, TypeScript strict, Tailwind, shadcn-compatible primitives,
TanStack Query, Recharts, Zod. Every API response is parsed with Zod before it
renders, so a shape change fails with one legible error instead of putting
`undefined` into a chart.

| Path | Contents |
|---|---|
| `app/forecast/page.tsx` | the Forecast Console |
| `app/scenario-planner/page.tsx` | the Scenario Planner |
| `components/forecast/` | controls, history input, KPIs, chart, table, export, notes |
| `components/layout/` | app shell, sidebar, topbar (live health) |
| `components/shared/` | metric card, status badge, empty / error / loading states |
| `lib/api.ts` | the typed client and the `ApiError` taxonomy |
| `lib/types.ts` | Zod schemas mirroring `api/schemas/` |
| `lib/sample-history.ts` | demo generator, CSV parser, history validation |

### ML Operations Console (`ui/`, Streamlit)

An internal operator view, kept deliberately distinct from the product UI: it
shows dependency probes, request ids, and raw response bodies. It loads no
model — the image installs no ML library and receives no object-store
credential, so direct artifact access is structurally impossible rather than
merely discouraged.

## 📈 Measured performance

From the latest training run (`67a0ca0922af`, 365 daily files → 3,650
store-days, 51 features, chronological 2555/365/730 split):

| Model | RMSE | MAE | R² | WAPE | MAPE |
|---|---|---|---|---|---|
| XGBoost | 82.96 | 31.57 | 0.995 | 1.12% | 153.6 |
| Ensemble | 98.08 | 29.64 | 0.993 | 1.05% | 202.0 |
| LightGBM | 118.56 | 32.21 | 0.990 | 1.14% | 250.7 |

**Read these with the caveat above.** `quantity_sold` in the training frame is
the target under another name, so the headline R² and WAPE largely measure that
identity rather than forecasting skill. MAPE is large because it is computed
only over non-zero actuals on a series containing many near-zero store-days —
which is why WAPE and bias are logged alongside it. Fixing the leakage and
retraining is required before these numbers mean what they appear to mean.

Pipeline timings on an 8 GB Docker allocation: training ~20–30 min with Optuna
(50 trials per model); forecast latency ~0.3–2 s for a 7–90 day horizon
(recursive, one model call per day); the first forecast after a restart adds a
one-off artifact download.

## 🐛 Troubleshooting

### Common Issues

| Symptom | Cause and fix |
|---|---|
| Services will not start | Docker memory below 8 GB; raise it in Docker Desktop |
| `container ... mlflow-1 is unhealthy` on start | MLflow `pip install`s at boot and can take minutes. The healthcheck allows 7 minutes (`start_period: 420s`); on a slow connection, wait and retry |
| Port already allocated | Another project owns 3000 or 8000. See [If 3000 or 8000 are already taken](#if-3000-or-8000-are-already-taken) |
| `No such image: ...-api:latest` | The API image has not been built. `docker compose build api`, then start again. (`pull_policy: build` normally handles this; the explicit `image:` tag keeps the name stable so the build is found.) |
| `streamlit-ui` reports unhealthy while the page loads fine | Was a probe bug — the healthcheck called `curl`, which `python:3.11-slim` does not ship. Fixed; if you see it again, check `docker inspect --format '{{json .State.Health}}'` for the probe's own output |
| Console says "No Production model available" | The DAG has not run, or promotion failed. Check `transition_to_production_task` in the Airflow UI, then `curl localhost:8000/v1/models` |
| Forecast fails with `INSUFFICIENT_HISTORY` | Fewer than `FORECAST_MIN_HISTORY_DAYS` (30) days supplied |
| Forecast fails with `ARTIFACT_DOWNLOAD_FAILED` | MinIO or the MLflow artifact proxy is unreachable; check `curl localhost:8000/health` |
| Browser shows a network error, API works via curl | CORS. The frontend origin must be listed in `API_CORS_ALLOW_ORIGINS` |
| `GET /v1/models` returns an empty list | Expected before the first training run. It is a 200 with a warning, not an error |

### Logs and debugging

```bash
astro dev logs                                   # Airflow
docker compose logs -f api                       # FastAPI (structured JSON)
docker compose logs -f mlflow streamlit-ui frontend

curl -s localhost:8000/health | python3 -m json.tool   # every dependency
curl -s localhost:8000/v1/models | python3 -m json.tool
```

Every API response carries an `X-Request-ID`, and both UIs display it. Grep the
API log for that id to find the matching server-side entry.

## 📚 Documentation

- [Forecast workflow](docs/FORECAST_WORKFLOW.md) — the end-to-end path, API contract, and limitations
- [Scenario Planner](docs/SCENARIO_PLANNER.md) — workflow, API contract, error codes, limitations
- [Training data sources](data/README.md) — pluggable providers and dataset versioning
- [Astronomer Docs](https://www.astronomer.io/docs/)

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
