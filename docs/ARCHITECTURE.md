# Architecture

How the platform is put together, and why. This document is the reference for
the boundaries that the code enforces: what the browser may reach, where
inference happens, who may do what, and where every privileged action is
recorded.

- [1. System architecture](#1-system-architecture)
- [2. The trust boundary](#2-the-trust-boundary)
- [3. Authentication flow](#3-authentication-flow)
- [4. Authorization — the RBAC matrix](#4-authorization--the-rbac-matrix)
- [5. Database schema](#5-database-schema)
- [6. Forecast workflow](#6-forecast-workflow)
- [7. Scenario workflow](#7-scenario-workflow)
- [8. Monitoring workflow](#8-monitoring-workflow)
- [9. Model registry workflow](#9-model-registry-workflow)
- [10. Audit logging](#10-audit-logging)
- [11. Layering rules inside the API](#11-layering-rules-inside-the-api)

---

## 1. System architecture

```
                         ┌──────────────────────────────┐
  browser  ──────────────▶  Next.js 14 (App Router)     │   port 3000
                         │  frontend/                   │
                         │  • access token in memory    │
                         │  • refresh cookie → /auth    │
                         └───────────────┬──────────────┘
                                         │  HTTPS/JSON — the ONLY backend
                                         │  the browser ever calls
                         ┌───────────────▼──────────────┐
  Streamlit ────────────▶│  FastAPI                     │   port 8000
  ML Operations Console  │  api/                        │
  port 8501              │  • auth, RBAC, audit         │
  (HTTP client only,     │  • feature engineering       │
  no ML libs installed)  │  • model load + inference    │
                         │  • monitoring + drift        │
                         │  • scenario engine           │
                         └──┬────────┬────────┬─────────┘
                            │        │        │
            ┌───────────────▼┐  ┌────▼─────┐ ┌▼──────────────┐
            │ PostgreSQL     │  │ MLflow   │ │ MinIO (S3)    │
            │ 13 tables:     │  │ tracking │ │ model         │
            │ users, audit,  │  │ +registry│ │ artifacts     │
            │ monitoring,    │  │ port 5001│ │ ports 9000/1  │
            │ scenarios      │  └────▲─────┘ └───▲───────────┘
            └───────▲────────┘       │           │
                    │                │           │
            ┌───────┴────────────────┴───────────┴───────────┐
            │ Airflow 3 (Astro CLI)                 port 8080│
            │  • sales_forecast_training   @weekly           │
            │      ingest → features → train → register      │
            │  • sales_forecast_monitoring @daily            │
            │      HTTP orchestrator → POST /v1/monitoring/  │
            │      evaluate as a service account             │
            └────────────────────────────────────────────────┘
```

### Why the monitoring DAG speaks HTTP instead of importing the API

Airflow 3 pins SQLAlchemy **1.4**; the API's models are SQLAlchemy **2.0**
declarative (`DeclarativeBase`, `Mapped`, `mapped_column`). Importing
`api.db` inside a DAG fails with `cannot import name 'DeclarativeBase'`, and
the conflict cannot be resolved in a shared image without downgrading one side.

So `dags/sales_forecast_monitoring.py` uses only `urllib` from the standard
library: it checks the API's health, signs in as the monitoring service
account, calls `POST /v1/monitoring/evaluate`, and reports the result. The
scheduler holds no database URL, no MLflow URI, and no object-store
credentials — just an API base URL and service-account credentials. The
evaluation logic lives in one place (`api/services/monitoring_evaluation.py`)
and is exercised by the same tests as the HTTP route.

---

## 2. The trust boundary

One rule governs the whole design:

> **MLflow, MinIO, PostgreSQL, AWS credentials, registry tokens, and model
> artifact paths are never exposed to the browser.**

Enforced structurally, not by convention:

| Component | What it holds | What it can reach |
|---|---|---|
| `frontend/` (browser) | `NEXT_PUBLIC_API_URL` only | FastAPI, nothing else |
| `ui/` (Streamlit) | `FORECAST_API_URL` only — and no `mlflow`, `boto3`, `scikit-learn`, `xgboost`, or `lightgbm` is installed in the image, so it *cannot* load an artifact even by mistake | FastAPI, nothing else |
| `api/` | MLflow URI, S3 endpoint + keys, database URL, JWT secret | everything |
| Airflow scheduler (monitoring) | API URL + service-account credentials | FastAPI |
| Airflow worker (training) | MLflow URI, S3 keys | MLflow, MinIO, data volume |

Verification is part of the test sweep: `docker exec <frontend> env` contains no
`JWT*`, `AWS*`, `MINIO*`, `*PASSWORD`, `*SECRET`, `DATABASE*`, or `MLFLOW*`
variable — only `NEXT_PUBLIC_API_URL`.

A second consequence: **all model loading and all inference happen inside the
FastAPI process.** Artifact bytes are fetched through MLflow's
`/get-artifact` HTTP proxy (so no object-store credential is required on the
inference path, and the fetch is immune to the MLflow client/server version
split), cached on disk at `MODEL_CACHE_DIR`, and unpickled with `include/` on
the import path so the cloudpickled ensemble resolves its classes.

---

## 3. Authentication flow

Tokens are JWT (HS256, PyJWT) with a `typ` claim that separates access from
refresh, plus verified `iss` and `aud`. Passwords are hashed with **bcrypt**
directly at `BCRYPT_ROUNDS` (default 12). Refresh tokens are stored only as
**SHA-256 digests** — the database never holds a usable token, and no password
or token is ever written to a log.

```
  POST /auth/login  { email, password }
        │
        │  1. look up user            ──▶ not found: verify against a cached
        │                                  real bcrypt hash anyway, so a
        │                                  missing account and a wrong password
        │                                  take the same time
        │  2. lockout check FIRST      ──▶ 423 ACCOUNT_LOCKED
        │     (before the password, so a locked account cannot be
        │      probed for password correctness)
        │  3. verify_password          ──▶ 401 INVALID_CREDENTIALS
        │                                  + failed_attempts += 1
        │  4. active check             ──▶ 403 ACCOUNT_INACTIVE
        ▼
  200  { access_token, expires_in, user{ id, email, role, permissions[] } }
  Set-Cookie: refresh_token=…; HttpOnly; SameSite=Lax; Path=/auth
```

- **Access token** — returned in the JSON body, held in **memory only** by the
  frontend (`lib/auth-store.ts`). Never in `localStorage`, so an XSS payload
  cannot read it out of storage. Default lifetime 45 min.
- **Refresh token** — an `HttpOnly` cookie scoped to `Path=/auth`, so it is not
  attached to any `/v1` request. Default lifetime 7 days. `Secure` is on by
  default and must only be disabled for plain-HTTP local development.

### Rotation and replay detection

`POST /auth/refresh` issues a new refresh token and revokes the one presented —
single use. Each token records the family it belongs to. **Presenting an
already-revoked token is treated as theft:** the whole family is revoked
(`reason=REVOKE_REUSE_DETECTED`), so both the attacker and the legitimate
holder are logged out and the event is auditable.

The frontend serialises refreshes through one shared in-flight promise, so a
page that fires six queries at once performs one refresh, not six — six would
rotate six times and trip replay detection against itself.

### Password changes and sessions

`POST /auth/change-password` requires the current password and **revokes every
refresh token for the user**, so a password change ends sessions everywhere.
`GET /auth/sessions` lists the caller's live sessions.

### First-run bootstrap

`api/bootstrap.py` runs in the lifespan handler. When the `users` table is
**empty**, it creates the `BOOTSTRAP_ADMIN_EMAIL` account as `ADMIN`; if
`BOOTSTRAP_ADMIN_PASSWORD` is unset, a strong password is generated and written
to the API log **once**. It also creates the monitoring service account
(`ML_ENGINEER`) when `MONITORING_SERVICE_PASSWORD` is set. Self-registration is
**disabled by default** (`ALLOW_SELF_REGISTRATION=false`); `GET /auth/config`
tells the login page which state it is in so the UI shows "Accounts are created
by an administrator" rather than a dead form.

`JWT_SECRET_KEY` has **no generated fallback**. A fallback would silently
invalidate every session on each restart and make two replicas reject each
other's tokens, so the API raises instead.

---

## 4. Authorization — the RBAC matrix

Authorization is a **capability** check, never a role check at the call site:

```python
user: User = Depends(require_permission(Permission.PROMOTE_MODEL))
```

A route that asks "is this an ADMIN or an ML_ENGINEER?" has to be found and
edited every time the role set changes. A route that asks "may this user
promote a model?" reads as its own documentation, and
[`api/core/rbac.py`](../api/core/rbac.py) is the one place a reviewer must read
to audit access.

Role sets are built by **union**, so privilege nests and a new read capability
is granted once instead of five times. `ADMIN` is `frozenset(Permission)` —
computed, so a newly added permission cannot be accidentally missing from the
administrator role.

### 5 roles × 18 permissions

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

Served live at `GET /v1/admin/permissions` and rendered on `/settings`, so the
documented table and the enforced rules cannot drift.

### Backend first, frontend second

The frontend hides what a user cannot do — but **hiding is presentation, not
security**. `GET /auth/me` returns the user's `permissions[]` from the same
matrix, and `frontend/lib/rbac.ts` uses that server-provided list to decide
which nav entries and buttons to render. Calling a forbidden endpoint directly
with a valid token still returns `403 PERMISSION_DENIED` — and writes a denial
audit row.

`get_current_user` re-loads the user from the database on every request rather
than trusting the token's claims, so a deactivation or role change takes effect
immediately instead of at token expiry, and it **fails closed** on a role name
it does not recognise.

---

## 5. Database schema

One PostgreSQL database, 13 tables, managed by Alembic. Types are portable —
`Uuid`, `JSON().with_variant(JSONB(), "postgresql")`,
`DateTime(timezone=True)` — so the suite can run on SQLite while production
runs on PostgreSQL. Because SQLite has no timezone type and returns naive
datetimes, every read passes through `as_utc()` in
[`api/db/base.py`](../api/db/base.py).

### Identity and sessions

| Table | Purpose | Notable columns |
|---|---|---|
| `users` | accounts | `email` (unique), `password_hash` (bcrypt), `role`, `is_active`, `failed_login_attempts`, `locked_until`, `last_login_at` |
| `refresh_tokens` | live sessions | `token_hash` (SHA-256, never the token), `family_id`, `revoked_at`, `revoked_reason`, `expires_at`, `user_agent`, `ip_address` |

### Audit

| Table | Purpose |
|---|---|
| `audit_logs` | append-only record: `actor_user_id`, `actor_email`, `actor_role`, `action`, `resource_type`, `resource_id`, `outcome` (`success`/`denied`/`failure`), `details` (JSONB), `ip_address`, `user_agent`, `created_at` |

### Monitoring

| Table | Purpose |
|---|---|
| `forecast_requests` | one row per forecast call: who, which model, horizon, store |
| `forecasts` | per-day predictions with their interval bounds, linked to the request |
| `actuals` | observed sales per store/day, ingested from the dataset |
| `monitoring_metrics` | scored windows: WAPE, MAE, RMSE, bias, masked MAPE, interval coverage, R² |
| `drift_metrics` | per-feature PSI and two-sample KS with their verdicts |
| `alerts` | raised alerts with a dedup `fingerprint`, severity, status (`open`/`acknowledged`/`resolved`) |

### Scenario planner

| Table | Purpose |
|---|---|
| `scenario_baseline_forecasts` | a baseline checkpoint a scenario is compared against, with a TTL |
| `scenario_forecasts` | one scenario comparison: levers, totals, deltas |
| `scenario_daily_forecasts` | per-day baseline vs scenario values |
| `scenario_audit_events` | the scenario's own lifecycle trail, including rollbacks |

### Migrations

`alembic.ini` + `api/migrations/`. `env.py` reads the URL from the environment
(`SCENARIO_DATABASE_URL`, then `DATABASE_URL`) rather than from the ini file,
so no credential is committed. The initial migration is **idempotent**: helper
wrappers (`_create_table`, `_create_index`, `_drop_index`, `_drop_table`) check
the live schema first, so it applies cleanly onto a database that `create_all`
already populated — the state every pre-Alembic deployment was in.

Verified end to end: fresh apply (14 tables incl. `alembic_version`, 75
indexes), re-run as a no-op, apply onto a `create_all` database, and
downgrade → re-upgrade.

---

## 6. Forecast workflow

```
POST /v1/forecast  ← require_permission(GENERATE_FORECAST)
  │
  ├─ 1. resolve model     model_registry_service: the Production version of
  │                       sales_forecast_models_<model>
  ├─ 2. load artifact     artifact_service: /get-artifact proxy → disk + LRU cache
  ├─ 3. validate history  feature_service: ≥ FORECAST_MIN_HISTORY_DAYS,
  │                       chronological, no duplicate dates
  ├─ 4. recursive walk    for each day in the horizon:
  │                       build calendar + lag + rolling features from the
  │                       path so far → predict → append → repeat
  ├─ 5. uncertainty       RMSE from the model's metrics → residual_normal;
  │                       otherwise history volatility. Band is
  │                       z × residual_std × √step
  └─ 6. persist + audit   forecast_requests + forecasts rows, so monitoring
                          can score this forecast later
```

The **feature contract** lives in exactly one place,
[`api/services/feature_service.py`](../api/services/feature_service.py), and is
shared by forecasting, scenarios, and monitoring. It reproduces the training
policy rather than inventing a serving-time one:

- `quantity_sold` and `profit` are same-day leaked features — reconstructed from
  the predicted value with the configured ratio, never asked of the caller.
- `customer_traffic` and `is_holiday` were effectively zero/constant in
  training, so serving supplies the same values; feeding realistic ones instead
  pushes the model out of distribution.
- `is_holiday` is computed from `holidays.US()`, not requested from the caller.

Matching the training policy is what removed the clipped "calibration" hack
that earlier versions needed: with it, a history averaging 2 977 produces
xgboost 3 058 / lightgbm 3 085 / ensemble 3 067.

Every response carries a `metadata` block (`uncertainty_method`,
`residual_std`, `imputed_features`, `run_id`, dataset lineage) and a
`warnings[]` list, so a caller is told what was assumed instead of having to
guess.

---

## 7. Scenario workflow

```
GET  /v1/scenario/capabilities      which scenario types this model supports
POST /v1/scenario/compare           ← require_permission(RUN_SCENARIO)
GET  /v1/scenario/{id}              a stored comparison
POST /v1/scenario/{id}/rollback     revert, preserving the audit history
GET  /v1/scenario/{id}/audit        the scenario's trail
```

1. A **baseline** forecast is checkpointed (`scenario_baseline_forecasts`) with
   a TTL, so the comparison is against a fixed, reproducible reference rather
   than a re-prediction that may have shifted.
2. `ScenarioMatrixBuilder` builds the future exogenous matrix and applies the
   levers to it.
3. `RecursiveMatrixPredictor` walks the horizon, **recomputing lag and rolling
   columns from its own evolving path** — which is what makes a multi-day
   scenario compound instead of applying a flat per-day offset. The matrix
   carries only exogenous overrides.
4. Baseline and scenario paths are stored per day, with totals and deltas, and
   every step is written to `scenario_audit_events`.

### Capability gating, and honest inertness

`GET /v1/scenario/capabilities` reads the model's feature schema and reports
which scenario types it can actually express. Two behaviours matter:

- **Unsupported** — `closure` and `price` have no corresponding feature in the
  current model, so they are refused with `409`. They are not silently
  approximated.
- **Supported but inert** — `promotion`, `traffic`, and `holiday` map to
  features that were effectively constant in the training data, so the model
  learned almost no response to them. The scenario runs and returns a
  near-zero delta **together with `INERT_LEVER_WARNING`**, which says the
  flatness is a property of the model, not a finding about the business.

That distinction is the point. Fabricating an uplift coefficient would make the
feature look useful and the number would be fiction.

When a model's `feature_schema.json` artifact is missing, the schema is derived
from `feature_cols.pkl` and marked `source_uri="derived:feature_cols.pkl"`, so
a consumer can tell a derived schema from a published one.

---

## 8. Monitoring workflow

```
  Airflow  sales_forecast_monitoring  @daily
      │  urllib only — no SQLAlchemy, no MLflow, no S3
      ├─ check_api      GET /health
      ├─ authenticate   POST /auth/login  (monitoring service account)
      ├─ evaluate       POST /v1/monitoring/evaluate
      └─ report         log the window, metrics, and alerts raised
                             │
                             ▼
             MonitoringEvaluator (api/services/monitoring_evaluation.py)
               1. ingest actuals from MONITORING_ACTUALS_PATH
               2. join forecasts ↔ actuals on (store, date)
               3. compute_accuracy  → monitoring_metrics
               4. evaluate_drift    → drift_metrics
               5. evaluate_alerts   → alerts (fingerprint-deduplicated)
```

**Accuracy.** WAPE is the headline because MAPE explodes on the zero-sales days
this dataset contains; MAPE is still reported, masked to non-zero actuals. Also
MAE, RMSE, bias (signed, so a systematic over-forecast is visible), interval
coverage (what fraction of actuals fell inside the predicted band), and R².

**Drift.** PSI and a two-sample KS test, with thresholds 0.10 / 0.25 (PSI) and
0.10 / 0.20 (KS), and a `MIN_DRIFT_SAMPLE` of 20 below which no verdict is
issued. A constant reference distribution is detected up front (`min == max`)
rather than discovered inside the bucketing.

What is measured is **prediction** drift — `forecast_error`,
`predicted_sales`, `actual_sales` — not input-feature drift, and the API says
so. Feature drift would require storing the per-request feature matrix.

**Alerts** carry a `fingerprint` that deliberately **excludes the measured
value**, so a metric that keeps breaching does not raise a new alert every day.
`manage_alerts` holders can acknowledge and resolve them.

**No fabrication.** With no scored window and no drift sample, the API returns
an explicit not-available state with the message:

> *Monitoring will populate after forecasts and actual outcomes are recorded.*

A retraining recommendation of `investigate` is returned when drift exists even
without a scorable accuracy window — otherwise a drifting model with no recent
actuals would report "no data" and look healthy.

---

## 9. Model registry workflow

```
GET  /v1/models                               versions currently serving
GET  /v1/models/compare?base=…&candidate=…    metric deltas, direction-aware
GET  /v1/models/{version}                     detail
GET  /v1/models/{version}/metrics
GET  /v1/models/{version}/lineage             run, dataset, parameters
GET  /v1/models/{version}/artifacts           artifact metadata (never bytes or paths)
POST /v1/models/{version}/promote             ← PROMOTE_MODEL
POST /v1/models/{version}/archive             ← ARCHIVE_MODEL
POST /v1/models/{version}/rollback            ← ROLLBACK_MODEL
```

Every mutation runs the same path: **confirm → apply → verify → audit →
re-read**. `_verify_stage()` re-reads the registry after the transition,
because a transition that reported success without being checked is exactly
what previously left the registry with *no* Production version at all.

Comparison is direction-aware: `LOWER_IS_BETTER` for error metrics,
`HIGHER_IS_BETTER` for R², so "improved" means improved rather than "went up".

### Two MLflow workarounds worth knowing

1. **`search_registered_models()` is not used.** A 3.x client against a 2.9.2
   server returns `[]` from it. The service enumerates the configured model
   names (`FORECAST_MODELS` × `MLFLOW_REGISTERED_MODEL_PREFIX`) instead, so the
   registry list does not silently come back empty.
2. **Stage transitions go through the REST API** —
   `POST /api/2.0/mlflow/model-versions/transition-stage` — followed by the
   verification re-read above.

---

## 10. Audit logging

`audit_logs` is **append-only**: no update or delete path exists in the
repository layer.

Recorded: authentication events, every privileged mutation (promote, archive,
rollback, user create/edit/deactivate/activate/reset-password, alert
acknowledge/resolve, scenario rollback), and **every authorization denial**.

Each row carries the actor (id, email, role — denormalised, so the trail stays
readable after a user is deleted), the action, the resource type and id, the
outcome, a JSONB `details` blob, the IP, and the user agent. Passwords and
tokens are never included.

### Why denials commit in their own transaction

`require_permission` audits the denial, then raises `403`. FastAPI's exception
handling rolls the request session back — and a row written into *that* session
would be discarded along with it. So `AuditService.record_denial` opens its own
`session_scope` and commits there. Without this, denials were silently missing.

Verified live: an `ANALYST` calling `POST /v1/models/{v}/promote` gets `403`
and leaves exactly one row with `outcome=denied`,
`details.needed=promote_model`.

`GET /v1/admin/audit-logs` is filterable by actor, action, resource, outcome,
and date range. `view_audit_logs` sees the trail; `view_all_audit_logs`
(ADMIN only) is the broader grant.

---

## 11. Layering rules inside the API

```
api/
├── routers/       HTTP only: parse, authorize, delegate, shape the response
├── services/      all business logic; no FastAPI imports
├── db/
│   ├── models/    SQLAlchemy 2.0 declarative
│   ├── repositories/  queries — take a Session, never create or commit one
│   └── session.py get_session (commit on success, rollback AND re-raise)
├── schemas/       Pydantic v2, extra="forbid"
├── core/          rbac, security, errors — no I/O
├── dependencies/  auth and service wiring
└── deps.py        lru_cache'd service singletons + reset_services()
```

Rules that hold throughout:

- **Repositories never own a transaction.** They take a `Session`. The request
  scope commits once on success, rolls back on failure — and **re-raises**,
  because swallowing the exception made FastAPI report "No response object was
  returned" instead of the real error.
- **Services never import FastAPI**, which is why the same evaluator serves
  both the HTTP route and the DAG.
- **Schemas are `extra="forbid"`**, so a typo'd field is a 422 rather than a
  silently ignored parameter.
- **Errors go through one envelope.** `api/core/errors.py` defines the codes;
  a `RequestValidationError` handler normalises schema-level 422s into the same
  shape, reporting the field location and message but **never echoing the
  caller's input** back.
- **CORS middleware is installed last**, so it is the outermost layer and an
  error response still carries its headers.
