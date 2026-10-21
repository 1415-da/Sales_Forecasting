# Astro Sales Forecasting MLOps Platform

An end-to-end retail sales forecasting platform: Airflow trains and registers
models, MLflow versions them, FastAPI serves them, and a Next.js web app gives
business users forecasts, what-if scenarios, monitoring, and a model registry —
behind authentication and role-based access control, with every privileged
action audited.

A user can sign in, pick a store and a horizon, supply history, and get a
forecast with an uncertainty band and a CSV download **without touching
Airflow, MLflow, MinIO, a notebook, or any code.**

```
browser ──▶ Next.js ──▶ FastAPI ──▶ MLflow · MinIO · PostgreSQL
                          ▲
            Airflow ──────┘  (training @weekly, monitoring @daily)
```

The browser talks to **one** backend. MLflow, MinIO, PostgreSQL, AWS
credentials, registry tokens, and model artifact paths are never exposed to it,
and every model load and every prediction happens inside the FastAPI process.

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [The web application](#the-web-application)
- [Roles and permissions](#roles-and-permissions)
- [Architecture](#architecture)
- [Forecasting](#forecasting)
- [Scenario planner](#scenario-planner)
- [Monitoring](#monitoring)
- [Model registry](#model-registry)
- [Authentication and audit](#authentication-and-audit)
- [ML pipeline](#ml-pipeline)
- [Measured performance](#measured-performance)
- [Tests](#tests)
- [Project layout](#project-layout)
- [Known limitations](#known-limitations)
- [Documentation](#documentation)
- [License](#license)

---

## What it does

| | |
|---|---|
| **Forecast Console** | multi-day sales forecasts per store, with an uncertainty band, KPIs, a daily table, and CSV export |
| **Scenario Planner** | predictive what-if: promotion, traffic, holiday, and custom levers against a checkpointed baseline, with per-day and total impact |
| **Monitoring** | WAPE/MAE/RMSE/bias/coverage/R² over a rolling window, PSI and KS drift, deduplicated alerts, data freshness, and a retraining recommendation |
| **Model Registry** | versions, metrics, lineage, artifact metadata, side-by-side comparison, and verified promote / archive / rollback |
| **Authentication** | JWT access tokens, rotating refresh tokens with replay detection, bcrypt passwords, lockout, first-run admin bootstrap |
| **RBAC** | 5 roles × 18 capabilities, enforced in the backend and mirrored in the UI |
| **Audit** | append-only trail of every privileged action **and every denial** |
| **Training** | Airflow DAG: ingest → features → train XGBoost / LightGBM / ensemble → log to MLflow → register |

### Technology

| Layer | Stack |
|---|---|
| Frontend | Next.js 14 (App Router), TypeScript strict, Tailwind, TanStack Query v5, Recharts, Zod |
| API | FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic, PyJWT, bcrypt |
| ML | scikit-learn, XGBoost (`xgboost-cpu`), LightGBM, pandas, NumPy |
| MLOps | Airflow 3 (Astro CLI), MLflow 2.9.2 (tracking + registry), MinIO (S3) |
| Data | PostgreSQL 16 ×2 (application, MLflow), Parquet dataset cache |
| Ops console | Streamlit — an API client with no ML library installed |

---

## Quick start

```bash
git clone <repo-url> && cd Sales_Forecasting
cp .env.example .env

# REQUIRED -- the API will not start without a signing key.
echo "JWT_SECRET_KEY=$(openssl rand -base64 48)" >> .env

astro dev start
```

| | |
|---|---|
| Web app | <http://localhost:3000> |
| API docs | <http://localhost:8000/docs> |
| Airflow | <http://localhost:8080> |
| MLflow | <http://localhost:5001> |
| Ops console | <http://localhost:8501> |
| MinIO console | <http://localhost:9001> |

Without the Astro CLI, plain Compose works too — `docker-compose.yml` is the
base file the CLI otherwise supplies in memory, with Airflow behind a profile:

```bash
docker compose up -d                    # application stack only
docker compose --profile airflow up -d  # the whole platform, Astro-free
```

**If 3000 or 8000 are taken**, all four variables move together — the browser's
API URL must match the API's port, and the API's allowed origins must match the
frontend's port:

```bash
API_PORT=8010 FRONTEND_PORT=3100 \
NEXT_PUBLIC_API_URL=http://localhost:8010 \
API_CORS_ALLOW_ORIGINS=http://localhost:3100,http://127.0.0.1:3100 \
astro dev start
```

### First sign-in

The first ADMIN is created automatically when the `users` table is empty. Set
`BOOTSTRAP_ADMIN_PASSWORD` in `.env`, or leave it unset and read the generated
one from the API log:

```bash
docker logs "$(docker ps --format '{{.Names}}' | grep -- '-api-1')" 2>&1 | grep -i bootstrap
```

Then sign in, change the password at `/settings`, and create accounts at
`/admin/users`. Self-registration is **off** by default.

### Get a model serving

A fresh deployment has no model in Production, and the console says so rather
than inventing a number. In the Airflow UI, enable and trigger
**`sales_forecast_training`**, then promote a version from `/models` (or the
MLflow UI). Enable **`sales_forecast_monitoring`** so accuracy and drift start
accumulating.

Full details, every environment variable, and troubleshooting:
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

---

## The web application

11 routes. The sidebar is built from the permission list the **server** returns
for your account, so you only see what you can use.

| Route | Page | Needs |
|---|---|---|
| `/` | redirect → `/forecast` | — |
| `/login` | sign in; states whether sign-up is open | — |
| `/register` | sign up, when `ALLOW_SELF_REGISTRATION=true` | — |
| `/forecast` | **Forecast Console** — store, horizon, model, confidence, history, chart + band, KPIs, table, CSV | `view_forecast` / `generate_forecast` |
| `/scenario-planner` | **Scenario Planner** — levers, baseline vs scenario, per-day impact | `view_scenario` / `run_scenario` |
| `/monitoring` | **Monitoring** — health, freshness, accuracy, drift, alerts | `view_monitoring` (+`_detail`) |
| `/models` | **Model Registry** — versions, metrics, lineage, compare, promote/archive/rollback | `view_model_registry` (+ mutations) |
| `/admin/users` | **Users** — create, edit, roles, activate/deactivate, reset password | `manage_users` |
| `/admin/audit-logs` | **Audit Log** — filterable, append-only, includes denials | `view_audit_logs` |
| `/settings` | profile, your exact permission list, change password, sessions | — |

Hiding a control is presentation only: the API enforces the same matrix, so
calling a forbidden endpoint with a valid token returns `403` **and writes a
denial to the audit log**.

Per-role walkthroughs: **[docs/USER_WORKFLOW.md](docs/USER_WORKFLOW.md)**.

---

## Roles and permissions

| Capability | ADMIN | ML_ENGINEER | ANALYST | BUSINESS_USER | VIEWER |
|---|:--:|:--:|:--:|:--:|:--:|
| `view_forecast` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `generate_forecast` | ✅ | ✅ | ✅ | ✅ | — |
| `export_forecast` | ✅ | ✅ | ✅ | ✅ | — |
| `run_scenario` | ✅ | ✅ | ✅ | ✅ | — |
| `view_scenario` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `view_model_performance` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `view_model_registry` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `view_model_registry_detail` | ✅ | ✅ | ✅ | — | — |
| `promote_model` | ✅ | ✅ | — | — | — |
| `archive_model` | ✅ | ✅ | — | — | — |
| `rollback_model` | ✅ | ✅ | — | — | — |
| `trigger_retraining` | ✅ | ✅ | — | — | — |
| `view_monitoring` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `view_monitoring_detail` | ✅ | ✅ | ✅ | — | — |
| `manage_alerts` | ✅ | ✅ | — | — | — |
| `manage_users` | ✅ | — | — | — | — |
| `view_audit_logs` | ✅ | ✅ | — | — | — |
| `view_all_audit_logs` | ✅ | — | — | — | — |

Defined once in [`api/core/rbac.py`](api/core/rbac.py), served live at
`GET /v1/admin/permissions`, and rendered on `/settings` — so this table and
the enforced rules cannot drift. Role sets are built by union so privilege
nests, and `ADMIN` is computed as `frozenset(Permission)` so a newly added
capability cannot be accidentally missing from it.

---

## Architecture

```
                         ┌──────────────────────────────┐
  browser  ──────────────▶  Next.js 14  (frontend/)     │   3000
                         │  access token in memory      │
                         │  refresh cookie → /auth      │
                         └───────────────┬──────────────┘
                                         │  the ONLY backend the browser calls
                         ┌───────────────▼──────────────┐
  Streamlit ────────────▶│  FastAPI  (api/)             │   8000
  ops console  8501      │  auth · RBAC · audit         │
  (API client; no ML     │  features · inference        │
  library installed)     │  monitoring · scenarios      │
                         └──┬────────┬────────┬─────────┘
            ┌───────────────▼┐  ┌────▼─────┐ ┌▼──────────────┐
            │ PostgreSQL     │  │ MLflow   │ │ MinIO (S3)    │
            │ 13 tables      │  │ 5001     │ │ 9000 / 9001   │
            └───────▲────────┘  └────▲─────┘ └───▲───────────┘
            ┌───────┴────────────────┴───────────┴───────────┐
            │ Airflow 3   sales_forecast_training   @weekly  │ 8080
            │             sales_forecast_monitoring @daily   │
            └────────────────────────────────────────────────┘
```

Inside the API: `routers/` (HTTP only) → `services/` (all logic, no FastAPI
imports) → `db/repositories/` (queries; never own a transaction) →
`db/models/` (SQLAlchemy 2.0). `core/` holds RBAC, security, and the error
contract with no I/O.

Two constraints shaped the design and are worth knowing before changing
anything:

- **The monitoring DAG speaks HTTP, not Python.** Airflow 3 pins SQLAlchemy 1.4
  while the API's models need 2.0, so importing `api.db` in a DAG fails
  outright. `dags/sales_forecast_monitoring.py` uses only `urllib`: it signs in
  as a service account and calls `POST /v1/monitoring/evaluate`. The scheduler
  therefore holds no database URL and no object-store credential.
- **MLflow 2.9.2 server vs 3.x client.** `search_registered_models()` returns
  `[]` across that split, so the registry service enumerates the configured
  model names instead. Artifacts are fetched through MLflow's `/get-artifact`
  proxy, which also means the inference path needs no object-store credential.

Full detail: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Forecasting

`POST /v1/forecast` → resolve the Production version → load the artifact
(proxy + disk cache) → validate the history → walk the horizon recursively,
rebuilding calendar, lag, and rolling features from the path so far → derive an
uncertainty band → persist the forecast so monitoring can score it later.

```json
{
  "store_id": "CA_1", "model": "ensemble",
  "horizon_days": 7, "confidence_level": 0.95,
  "history": [{ "date": "2016-01-01", "sales": 2620.0 }, "… ≥ 30 days"]
}
```

```json
{
  "summary": { "total_forecast_sales": 21650.87, "average_daily_sales": 3092.98,
               "lower_bound_total": 19060.15, "upper_bound_total": 24241.6 },
  "forecasts": [{ "date": "2016-02-15", "predicted_sales": 3094.89,
                  "lower_bound": 2902.66, "upper_bound": 3287.11 }],
  "metadata": { "uncertainty_method": "residual_normal", "residual_std": 98.0759,
                "imputed_features": ["quantity_sold", "profit", "customer_traffic", "has_promotion"],
                "feature_count": 51, "run_id": "67a0ca09…", "dataset_version": "v1.0.0" },
  "warnings": ["…"]
}
```

### The feature contract, and why the request looks sparse

You are not asked for `is_holiday`, `customer_traffic`, `quantity_sold`, or
`profit`. One module —
[`api/services/feature_service.py`](api/services/feature_service.py) — owns the
contract for forecasting, scenarios, and monitoring alike, and it **reproduces
the training policy** rather than inventing a serving-time one:

- `quantity_sold` and `profit` are same-day leaked columns, reconstructed from
  the predicted value;
- `customer_traffic` and `has_promotion` were effectively constant in training,
  so serving supplies the same values — feeding "realistic" ones would push the
  model out of distribution;
- `is_holiday` is computed from `holidays.US()`.

Matching training is what removed the clipped calibration hack earlier versions
needed: a history averaging 2 977 now yields xgboost 3 058 / lightgbm 3 085 /
ensemble 3 067, with no post-hoc correction.

Every response carries `metadata` and `warnings[]`, so a caller is told what was
assumed instead of guessing. The band is `z × residual_std × √step`, which is
why it widens across the horizon — later days are forecast from earlier
forecasts.

---

## Scenario planner

`POST /v1/scenario/compare` checkpoints a **baseline** forecast, applies the
levers to the future exogenous matrix, and re-walks the horizon with a
predictor that recomputes lag and rolling columns from its own evolving path —
which is what makes a multi-day scenario **compound** rather than apply a flat
daily offset.

Two behaviours matter more than the feature list:

**Unsupported levers are refused, not approximated.** `closure` and `price` have
no corresponding feature in the current model:

```json
{ "code": "SCENARIO_NOT_SUPPORTED",
  "message": "Model version latest was not trained with any of: is_closed, store_event.",
  "details": { "missing_features": ["is_closed", "store_event"],
               "supported_scenario_types": ["promotion", "traffic", "holiday", "custom"] } }
```

**Inert levers are disclosed, not dressed up.** A live 7-day high-intensity
promotion over a 14-day horizon moves the total by **−0.38 units (−0.0009%)**,
and the response says why:

> *The scenario changed the model's inputs but barely moved its output. The
> feature it targets was effectively constant in this model's training data, so
> the model has no fitted response to it. Read this as 'this model cannot
> express that lever', not as 'the change would have no business effect'.*

Partial application is reported too: `applied_overrides` lists what was set
(`has_promotion` → 1 across 7 dates, with before/after value summaries), and
`unsupported_overrides` lists what the schema has no column for
(`discount_depth`), with a reason. Nothing is silently dropped, and no uplift
coefficient is invented to make the number look useful.

---

## Monitoring

```
Airflow @daily ─ urllib only ─▶ POST /v1/monitoring/evaluate
                                  ingest actuals → join forecasts ↔ actuals
                                  → accuracy → drift → alerts
```

**Accuracy.** WAPE is the headline because MAPE explodes on this dataset's
zero-sales days (it is still reported, masked to non-zero actuals). Also MAE,
RMSE, signed bias, **interval coverage**, and R², with a window-over-window
trend.

**Drift.** PSI and two-sample KS, thresholds 0.10/0.25 and 0.10/0.20, no
verdict below 20 samples. What is measured is **prediction** drift
(`forecast_error`, `predicted_sales`, `actual_sales`), not input-feature drift,
and the API says so rather than implying more.

**Alerts** are fingerprint-deduplicated — the fingerprint deliberately excludes
the measured value, so a persistent breach updates one alert
(`occurrence_count`) instead of raising a new one daily.

**Nothing is fabricated.** With no scored window the response is explicit:

> *Monitoring will populate after forecasts and actual outcomes are recorded.*

And when drift exists but accuracy cannot be measured, the retraining
recommendation is `investigate` rather than `no_data` — otherwise a drifting
model with stale actuals would look healthy.

---

## Model registry

`/models` lists versions with metrics, lineage, and artifact **metadata**
(never bytes, never object-store paths), compares two versions with
direction-aware deltas, and promotes / archives / rolls back.

Every mutation runs **confirm → apply → verify → audit → re-read**. The verify
step re-reads the registry after the transition, because a transition that
reported success without being checked is exactly what previously left the
registry with no Production version at all. Verification failure returns `502
REGISTRY_OPERATION_FAILED` — not a success.

---

## Authentication and audit

JWT (HS256) with a `typ` claim separating access from refresh and verified
`iss`/`aud`. Passwords are bcrypt at `BCRYPT_ROUNDS`. Refresh tokens are stored
only as **SHA-256 digests**, so the database never holds a usable token, and no
password or token is ever logged.

| | |
|---|---|
| Access token | in the response body, held in **memory only** by the frontend — never `localStorage` |
| Refresh token | `HttpOnly`, `SameSite=Lax`, `Path=/auth`, so it is never attached to a `/v1` request |
| Rotation | single-use; **replaying a revoked token revokes the whole family** |
| Lockout | after `MAX_LOGIN_ATTEMPTS`, and checked **before** the password so a locked account is not a password oracle |
| Enumeration | a missing account is verified against a real cached bcrypt hash, so timing matches a wrong password |
| Revocation | every request re-loads the user from the database, so deactivation and role changes apply immediately — and an unrecognised role fails closed |
| Password change | revokes every session |
| Lockout guards | you cannot change your own role, deactivate yourself, or remove the last active ADMIN |

`audit_logs` is append-only and records authentication, every privileged
mutation, and **every authorization denial**. Denials commit in their own
transaction: `require_permission` audits and then raises `403`, FastAPI rolls
the request session back, and a row written into *that* session would be
discarded with it. Without this, denials were silently missing.

---

## ML pipeline

`dags/sales_forecast_training.py`, `@weekly`:

1. **Ingest** — pluggable data provider (`include/data_providers/`); the local
   M5 provider reads the Parquet cache in `data/raw` and records dataset
   lineage (`data_source`, `dataset_version`) that every forecast carries
   forward.
2. **Features** — 51 columns: lags, rolling windows (ddof=1), calendar,
   holidays, store encoding.
3. **Train** — XGBoost, LightGBM, and a cloudpickled ensemble, with a
   `StandardScaler`.
4. **Log and register** — params, metrics, and artifacts to MLflow; versions
   registered as `sales_forecast_models_<model>`.

`dags/sales_forecast_monitoring.py`, `@daily`: the HTTP orchestrator described
above.

The Streamlit **ML Operations Console** (`ui/`) is an API client. Its image
installs no `mlflow`, `boto3`, `scikit-learn`, `xgboost`, or `lightgbm`, which
is what makes direct artifact access impossible rather than merely
discouraged.

---

## Measured performance

Training metrics, as published by MLflow for the serving versions:

| Model | WAPE | MAE | RMSE | R² | Bias |
|---|---|---|---|---|---|
| ensemble | 1.05% | 29.64 | 98.08 | 0.993 | +6.75 |
| xgboost | 1.12% | 31.57 | 82.96 | 0.995 | +3.67 |

**Those numbers overstate accuracy badly.** A real out-of-sample backtest
measured:

| Metric | Backtest | Training claim |
|---|---|---|
| WAPE | **18.2%** | 1.1% |
| MAE | **779** | 29.6 |
| RMSE | **956** | 98.1 |
| Bias | **+292** | +6.7 |
| R² | **0.458** | 0.995 |
| 95% interval coverage | **55%** | — |

The cause is target leakage in the feature set: `quantity_sold` *equals* `sales`
and `profit` is exactly `0.3 × sales`. Those columns cannot be known for a
future date, so serving reconstructs them from the last observed day — which is
why a forecast reads as a level projection and why the training metrics are not
reproducible out of sample.

The platform reports this rather than hiding it: `/v1/monitoring/accuracy`
carries the measured numbers, and monitoring raised the coverage alert on its
own —

> *Interval coverage is 55%. The 95% interval contained the actual 55% of the
> time, so it is narrower than it claims.*

Drift, measured live: PSI `forecast_error` **2.59** and `predicted_sales`
**6.44** (both drifting); `actual_sales` **0.057** (stable).

Trust the monitoring numbers, not the training card.

---

## Tests

```bash
# Backend -- 710 tests: services, routers, error contract, auth, RBAC,
# monitoring, admin, scenario planner, dataset lineage
pytest api/tests tests/api tests/test_dataset_lineage.py -q

# Frontend -- 278 tests + strict typecheck + production build (11 routes)
cd frontend && npx tsc --noEmit && npm test && npm run build

# Live suite against a running stack -- 10 tests; skips without the password
LIVE_API_URL=http://localhost:8000 LIVE_ADMIN_PASSWORD=… npm test -- live-api
```

The backend suite overrides authentication at the `get_current_user` seam only,
so **permission checks still execute for real** — the RBAC tests assert actual
403s rather than a mocked decision. The full matrix was additionally verified
live across all five roles.

---

## Project layout

```
api/
├── routers/        auth, forecast, scenario, monitoring, models, admin_*, health
├── services/       forecast, feature, artifact, model_registry, registry_admin,
│                   scenario_*, monitoring_*, auth, user, audit
├── db/
│   ├── models/     users, refresh_tokens, audit_logs, monitoring, scenarios
│   ├── repositories/
│   └── session.py
├── schemas/        Pydantic v2, extra="forbid"
├── core/           rbac.py · security.py · errors.py
├── dependencies/   auth.py (require_permission) · scenario.py
├── migrations/     Alembic (idempotent initial revision)
├── bootstrap.py    first-run admin + monitoring service account
└── tests/

frontend/
├── app/            11 routes (App Router)
├── components/     forecast · scenario · monitoring · registry · admin · auth · layout
├── hooks/          use-auth · use-forecast · use-models · use-monitoring · use-registry · use-admin
├── lib/            api · auth-store · rbac · types · format
└── tests/

docker-compose.yml           base: airflow network, metadata db, Airflow components
docker-compose.override.yml  app services + Astro's Airflow volumes/env
dags/               sales_forecast_training · sales_forecast_monitoring
include/            training code, data providers, feature engineering
ui/                 Streamlit ML Operations Console (API client only)
docs/               ARCHITECTURE · USER_WORKFLOW · API · DEPLOYMENT · FORECAST_WORKFLOW · SCENARIO_PLANNER
```

---

## Known limitations

Honest list, in rough order of how much they matter:

1. **Target leakage is unfixed.** `quantity_sold == sales` and
   `profit == 0.3 × sales` are in the training feature set. Fixing it needs a
   feature allowlist change in `include/` and a retrain; the 18.2% backtest
   WAPE is what to expect, and may improve once the model is forced to learn
   from non-leaked signal.
2. **The 95% interval is over-confident** — 55% measured coverage. The residual
   estimate comes from the (leaked) training RMSE, so it is too small. A
   conformal or backtest-derived residual would fix it.
3. **Scenario levers are near-inert for this model**, because
   `has_promotion`, `customer_traffic`, and `is_holiday` were effectively
   constant in training. The platform discloses this; making the levers
   meaningful requires training data in which they actually vary.
4. **`closure` and `price` scenarios are unsupported** — no corresponding
   feature exists. Correctly refused with `409` rather than approximated.
5. **Monitoring shows the no-data state by default.** The M5 actuals end
   **2016-04-24**, so a rolling 28-day window around today has no scorable
   pairs; real numbers come from an explicit backtest window.
6. **Drift is prediction drift, not feature drift.** Feature drift would need
   the per-request feature matrix persisted.
7. **`max_sales_files: 365` truncates training data** in the ingestion config.
8. **No CI.** Python lint, frontend lint, and Docker build checks are not wired
   into a pipeline yet; all three run clean locally.
9. **No SSO/OIDC, MFA, per-user API keys, or global rate limiting** beyond
   login lockout.
10. **The Airflow image carries a stale baked-in copy of `api/`** — harmless
    now that no DAG imports it, but it should be dropped from the image.
11. **The frontend runs `next dev`** in compose. For production, build the app
    and serve `next start`.
12. **MLflow, MinIO, and Airflow have no authentication** in this compose
    setup. Do not publish 5001, 9000, 9001, or 8080 beyond localhost.

---

## Documentation

| Document | Contents |
|---|---|
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | system architecture, trust boundary, auth flow, RBAC, database schema, forecast / scenario / monitoring / registry workflows, audit logging, API layering rules |
| **[docs/USER_WORKFLOW.md](docs/USER_WORKFLOW.md)** | per-role walkthroughs: business user, analyst, ML engineer, administrator, viewer |
| **[docs/API.md](docs/API.md)** | every endpoint with its permission, plus request/response examples captured from the running stack |
| **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** | Docker setup, services and ports, every environment variable, initial admin, migrations, security checklist, backup, troubleshooting |
| **[docs/FORECAST_WORKFLOW.md](docs/FORECAST_WORKFLOW.md)** | the forecast path in depth, including the feature contract |
| **[docs/SCENARIO_PLANNER.md](docs/SCENARIO_PLANNER.md)** | the scenario engine, overrides, and capability gating |

Interactive API docs: <http://localhost:8000/docs>.

---

## License

See [LICENSE](LICENSE).
