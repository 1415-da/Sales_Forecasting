# API reference

FastAPI, served at `http://localhost:8010` in this workspace. Interactive docs
are at `/docs`, and the machine schema is at `/openapi.json`.

**Every example below was captured from the running stack.** Nothing here is
illustrative-only.

- [Conventions](#conventions)
- [Errors](#errors)
- [Authentication](#authentication-auth)
- [Forecast](#forecast-v1forecast)
- [Scenario planner](#scenario-planner-v1scenario)
- [Monitoring](#monitoring-v1monitoring)
- [Model registry](#model-registry-v1models)
- [Administration](#administration-v1admin)
- [Health](#health)
- [Permission index](#permission-index)

---

## Conventions

| | |
|---|---|
| **Auth** | `Authorization: Bearer <access_token>` on every `/v1` route. `/health`, `/metrics`, `/auth/config`, `/auth/login`, `/auth/register`, `/auth/refresh` are the only open routes. |
| **Refresh** | an `HttpOnly` `SameSite=Lax` cookie scoped to `Path=/auth`, so it is not attached to `/v1` calls. Send `POST /auth/refresh` with credentials included. |
| **Bodies** | JSON. Schemas are `extra="forbid"` — an unknown field is a `422`, never silently dropped. |
| **Dates** | `YYYY-MM-DD`. Timestamps are ISO-8601 UTC with a `Z` suffix. |
| **Success flag** | every response carries `success: true|false`. |
| **Request id** | every error carries a `request_id`, matching the API log line. |

---

## Errors

One envelope for everything, including schema-level `422`s:

```json
{
  "success": false,
  "error": {
    "code": "PERMISSION_DENIED",
    "message": "You do not have permission to perform this action.",
    "field": null,
    "details": { "required": ["manage_users"], "your_role": "ML_ENGINEER" },
    "request_id": "req_38b0e39c35a34eecbe25087428edfd47",
    "timestamp": "2026-10-21T10:56:18Z"
  }
}
```

A `422` reports the field **location and message only** — the caller's input is
never echoed back.

| Code | Status | Meaning |
|---|---|---|
| `NOT_AUTHENTICATED` | 401 | no or malformed token |
| `INVALID_CREDENTIALS` | 401 | wrong email or password |
| `TOKEN_EXPIRED` / `TOKEN_INVALID` | 401 | refresh and retry / sign in again |
| `ACCOUNT_INACTIVE` | 403 | deactivated account |
| `ACCOUNT_LOCKED` | 423 | too many failed attempts; retry after the lockout |
| `PERMISSION_DENIED` | 403 | role lacks the capability — **recorded in the audit log** |
| `REGISTRATION_DISABLED` | 403 | `ALLOW_SELF_REGISTRATION=false` |
| `EMAIL_ALREADY_REGISTERED` | 409 | duplicate email |
| `WEAK_PASSWORD` | 422 | `details` lists **every** failed rule, not just the first |
| `RATE_LIMITED` | 429 | too many attempts |
| `USER_NOT_FOUND` / `ALERT_NOT_FOUND` | 404 | unknown id |
| `INSUFFICIENT_HISTORY` | 422 | fewer than `FORECAST_MIN_HISTORY_DAYS` days |
| `FEATURE_MISMATCH` | 409 | history cannot satisfy the model's feature schema |
| `MODEL_NOT_AVAILABLE` | 503 | no version in the required stage |
| `SCENARIO_NOT_SUPPORTED` | 409 | the model has no feature for that lever |
| `MONITORING_DATA_UNAVAILABLE` | 200/409 | see the no-data contract below |
| `REGISTRY_OPERATION_FAILED` | 502 | MLflow rejected the transition, or verification failed |
| `INVALID_STATE_TRANSITION` | 409 | e.g. promoting an archived version |

---

## Authentication (`/auth`)

| Method | Path | Permission |
|---|---|---|
| GET | `/auth/config` | open |
| POST | `/auth/register` | open, and only when self-registration is on |
| POST | `/auth/login` | open |
| POST | `/auth/refresh` | refresh cookie (or body token) |
| POST | `/auth/logout` | authenticated |
| GET | `/auth/me` | authenticated |
| POST | `/auth/change-password` | authenticated |
| GET | `/auth/sessions` | authenticated |

### `GET /auth/config`

What the login page needs before anyone is signed in — whether sign-up is open,
and the password rules — so the UI never shows a form that cannot work.

### `POST /auth/login`

```json
{ "email": "monitoring-service@example.com", "password": "…" }
```

```json
{
  "success": true,
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…",
  "token_type": "bearer",
  "expires_in": 2699,
  "user": {
    "id": "6db91bd0-e291-4d00-885c-57e5a9ae8db6",
    "email": "monitoring-service@example.com",
    "full_name": "Monitoring Service",
    "role": "ML_ENGINEER",
    "role_description": "Promote, archive, and roll back models; trigger retraining; manage alerts; view audit logs. Cannot manage users.",
    "is_active": true,
    "must_change_password": false,
    "permissions": ["archive_model", "export_forecast", "generate_forecast", "…"]
  }
}
```

`Set-Cookie: refresh_token=…; HttpOnly; SameSite=Lax; Path=/auth` accompanies
it. `permissions[]` comes from the same matrix the API enforces, so a client can
render role-aware UI without hardcoding a second copy.

Failure behaviour worth knowing:

- a **missing account** is verified against a cached real bcrypt hash anyway, so
  it takes the same time as a wrong password — the endpoint is not a user
  enumeration oracle;
- the **lockout is checked before the password**, so a locked account cannot be
  probed for password correctness;
- `423 ACCOUNT_LOCKED` after `MAX_LOGIN_ATTEMPTS` (default 8) for
  `LOGIN_LOCKOUT_MINUTES` (default 15).

### `POST /auth/refresh`

Rotates: a new refresh token is issued and the presented one is revoked —
single use. **Replaying an already-revoked token revokes the entire token
family** (`REVOKE_REUSE_DETECTED`), logging out both the attacker and the
legitimate holder. Clients must serialise concurrent refreshes through one
in-flight request, or they will trip this against themselves.

### `POST /auth/change-password`

Requires `current_password`. Enforces the strength rules — `details` lists
**every** violated rule at once. **Revokes every refresh token for the user**,
so a password change ends sessions everywhere.

---

## Forecast (`/v1/forecast`)

| Method | Path | Permission |
|---|---|---|
| POST | `/v1/forecast` | `generate_forecast` |

### Request

```json
{
  "store_id": "CA_1",
  "model": "ensemble",
  "horizon_days": 7,
  "confidence_level": 0.95,
  "history": [
    { "date": "2016-01-01", "sales": 2620.0 },
    { "date": "2016-01-02", "sales": 2688.0 },
    "… at least FORECAST_MIN_HISTORY_DAYS (30) days, in order, no duplicates"
  ]
}
```

| Field | Rules |
|---|---|
| `store_id` | one of the model's `supported_stores` |
| `model` | `ensemble` (default) \| `xgboost` \| `lightgbm` — from `FORECAST_MODELS` |
| `horizon_days` | 1 … `FORECAST_MAX_HORIZON_DAYS` (90) |
| `confidence_level` | `0.8` or `0.95` — nothing else is accepted |
| `history[].date` | strictly increasing, no duplicates |
| `history[].sales` | ≥ 0 |

There is **no `is_holiday` field by design**, and `quantity_sold`, `profit`,
and `customer_traffic` are not requested either: the API derives all of them
exactly as training did. Supplying "realistic" values instead would push the
model out of the distribution it was fitted on.

### Response (live, 7-day horizon, 45 days of history)

```json
{
  "forecast_id": "fc_755f626b13614605",
  "store_id": "CA_1",
  "model": "ensemble",
  "model_version": "1",
  "model_stage": "Production",
  "generated_at": "2026-10-21T10:55:30.813976Z",
  "horizon_days": 7,
  "confidence_level": 0.95,
  "summary": {
    "total_forecast_sales": 21650.87,
    "average_daily_sales": 3092.98,
    "lower_bound_total": 19060.15,
    "upper_bound_total": 24241.6
  },
  "forecasts": [
    { "date": "2016-02-15", "predicted_sales": 3094.89, "lower_bound": 2902.66, "upper_bound": 3287.11 },
    { "date": "2016-02-16", "predicted_sales": 3097.78, "lower_bound": 2825.94, "upper_bound": 3369.63 },
    { "date": "2016-02-21", "predicted_sales": 3087.94, "lower_bound": 2579.36, "upper_bound": 3596.52 }
  ],
  "metadata": {
    "history_start_date": "2016-01-01",
    "history_end_date": "2016-02-14",
    "history_days": 45,
    "feature_schema_version": "v1",
    "feature_count": 51,
    "uncertainty_method": "residual_normal",
    "residual_std": 98.0759,
    "imputed_features": ["quantity_sold", "profit", "customer_traffic", "has_promotion"],
    "run_id": "67a0ca0922af4ea289f76a9a943ecafd",
    "data_source": "kaggle_m5",
    "dataset_version": "v1.0.0"
  },
  "warnings": [
    "45 days of history supplied; 60 or more gives the rolling features a fuller window.",
    "This model was trained with same-day columns that are algebraically tied to the target (quantity_sold equals sales and profit is a fixed share of it). They cannot be known for a future date, so they are reconstructed from the most recent observed day. The forecast therefore stays close to the recent sales level and should be read as a level projection rather than an independent prediction of day-to-day movement.",
    "Days beyond the first are forecast from earlier forecasts, so uncertainty grows across the horizon."
  ]
}
```

The band widens with the horizon because it is `z × residual_std × √step`, and
`uncertainty_method` names where `residual_std` came from — `residual_normal`
when the model published an RMSE, `history_volatility` otherwise.

Note the interval's **measured** coverage is 55%, not 95% — see
[§ Monitoring](#monitoring-v1monitoring).

---

## Scenario planner (`/v1/scenario`)

| Method | Path | Permission |
|---|---|---|
| GET | `/v1/scenario/capabilities` | `view_scenario` |
| GET | `/v1/scenario/baseline/{baseline_forecast_id}` | `view_scenario` |
| POST | `/v1/scenario/compare` | `run_scenario` |
| GET | `/v1/scenario/{scenario_id}` | `view_scenario` |
| POST | `/v1/scenario/{scenario_id}/rollback` | `run_scenario` |
| GET | `/v1/scenario/{scenario_id}/audit` | `view_audit_logs` |

### `GET /v1/scenario/capabilities`

Which scenario types the selected model can actually express, and why not for
the rest. Ask this before rendering the lever UI. For the current model:
`promotion`, `traffic`, `holiday`, `custom` are available; `closure` and
`price` are not.

### `POST /v1/scenario/compare`

```json
{
  "store_id": "CA_1",
  "horizon_days": 14,
  "history": [ { "date": "2016-01-01", "sales": 2620.0 }, "… ≥ 7 points" ],
  "scenario": {
    "scenario_type": "promotion",
    "start_date": "2016-02-16",
    "end_date": "2016-02-22",
    "promotion_intensity": "high",
    "discount_depth": 0.2
  }
}
```

| Field | Notes |
|---|---|
| `model_version` | default `"latest"` |
| `horizon_days` | 1…90, default 30 |
| `confidence_levels` | quantiles to report |
| `baseline_forecast_id` | reuse an existing checkpoint instead of recomputing |
| `scenario.scenario_type` | `promotion` \| `traffic` \| `closure` \| `holiday` \| `price` \| `custom` |
| `scenario.promotion_intensity` | `none` \| `low` \| `medium` \| `high` |
| `scenario.traffic_multiplier` | 0.5 … 2.0 |
| `scenario.closure_dates` | up to 90 dates |
| `scenario.custom_overrides` | feature → value |

### Response (live)

```json
{
  "scenario_id": "scn_21dc871a2350483db41e1fdee04c287b",
  "baseline_forecast_id": "bf_217833c194a04d528fcdd3e8fa5a4df7",
  "scenario_forecast_id": "sf_c9f8b241754949fd837058091f5aeb2d",
  "model_version": "1",
  "model_stage": "Production",
  "scenario_type": "promotion",
  "forecast_type": "predictive_what_if",
  "baseline":  { "horizon_days": 14, "total_forecast": 43304.12, "average_daily_forecast": 3093.15, "uncertainty_method": "residual_based", "has_quantiles": true },
  "scenario":  { "horizon_days": 14, "total_forecast": 43303.74, "average_daily_forecast": 3093.12, "uncertainty_method": "residual_based", "has_quantiles": true },
  "impact": {
    "total_baseline_forecast": 43304.12,
    "total_scenario_forecast": 43303.74,
    "total_difference": -0.3769,
    "total_difference_pct": -0.00087,
    "positive_days": 0, "negative_days": 7, "neutral_days": 7,
    "peak_impact_date": "2016-02-18",
    "peak_impact_value": -0.0538,
    "dates": ["2016-02-15", "…"],
    "daily_difference": [0.0, -0.0538, "…"],
    "cumulative_difference": [0.0, -0.0538, "…"],
    "quantile_impacts": "…", "impact_range": "…", "daily_impacts": "…"
  },
  "daily_forecasts": [
    {
      "date": "2016-02-16",
      "baseline_forecast": 3097.7847, "scenario_forecast": 3097.7308,
      "daily_difference": -0.0538, "daily_difference_pct": -0.00174,
      "baseline_p10": 2800.9432, "baseline_p50": 3097.7847, "baseline_p90": 3394.6262,
      "scenario_p10": 2800.8893, "scenario_p50": 3097.7308, "scenario_p90": 3394.5723
    }
  ],
  "applied_overrides": [
    {
      "feature_name": "has_promotion",
      "override_type": "set_constant",
      "affected_date_count": 7,
      "original_value_summary": { "count": 7, "min": 0.0, "max": 0.0, "mean": 0.0, "distinct": [0.0] },
      "new_value_summary":      { "count": 7, "min": 1.0, "max": 1.0, "mean": 1.0, "distinct": [1.0] },
      "detail": "set to 1"
    }
  ],
  "unsupported_overrides": [
    {
      "feature_name": "discount_depth",
      "reason": "'discount_depth' is not in the model feature schema, so it was not available during training.",
      "error_code": "FEATURE_NOT_AVAILABLE"
    }
  ],
  "modified_features": ["has_promotion"],
  "modified_dates": ["2016-02-16", "…", "2016-02-22"],
  "limitations": [
    "This is a predictive what-if estimate based on the selected production model.",
    "It does not establish causal impact.",
    "Assumes scenario effects resemble historical patterns.",
    "Uncertainty reflects model error and does not include external shocks."
  ],
  "warnings": [
    "Some requested overrides were not applied because the model's feature schema does not contain them; the scenario is therefore partial.",
    "The scenario changed the model's inputs but barely moved its output. The feature it targets was effectively constant in this model's training data, so the model has no fitted response to it. Read this as 'this model cannot express that lever', not as 'the change would have no business effect'."
  ],
  "baseline_reused": false,
  "schema_version": "v1",
  "generated_at": "2026-10-21T10:55:53.396319Z"
}
```

Two deliberate behaviours are visible in that real response:

1. **Partial application is disclosed.** `has_promotion` was applied;
   `discount_depth` has no feature in the schema, so it lands in
   `unsupported_overrides` with a reason — rather than being quietly ignored.
2. **Inertness is disclosed.** A 7-day high-intensity promotion moves a 14-day
   total by **−0.38 units (−0.0009%)**. The warning states that this is the
   model's lack of a fitted response, not a business finding. No uplift
   coefficient is invented to make the number look meaningful.

### Unsupported scenario (live `409`)

```json
{
  "success": false,
  "error": {
    "code": "SCENARIO_NOT_SUPPORTED",
    "message": "Model version latest was not trained with any of: is_closed, store_event.",
    "field": "scenario.scenario_type",
    "details": {
      "scenario_type": "closure",
      "model_version": "latest",
      "missing_features": ["is_closed", "store_event"],
      "supported_scenario_types": ["promotion", "traffic", "holiday", "custom"]
    },
    "request_id": "req_a05a0bb96127449a9686742d1983ff17",
    "timestamp": "2026-10-21T10:55:53Z"
  }
}
```

---

## Monitoring (`/v1/monitoring`)

| Method | Path | Permission |
|---|---|---|
| GET | `/v1/monitoring/summary` | `view_monitoring` |
| GET | `/v1/monitoring/accuracy` | `view_monitoring` |
| GET | `/v1/monitoring/data-freshness` | `view_monitoring` |
| GET | `/v1/monitoring/drift` | `view_monitoring_detail` |
| GET | `/v1/monitoring/alerts` | `view_monitoring` |
| POST | `/v1/monitoring/alerts/{alert_id}/acknowledge` | `manage_alerts` |
| POST | `/v1/monitoring/alerts/{alert_id}/resolve` | `manage_alerts` |
| POST | `/v1/monitoring/evaluate` | `manage_alerts` |

Common query parameters: `window_days` (default `MONITORING_WINDOW_DAYS`, 28),
`store_id`, `model_version`.

### `GET /v1/monitoring/summary` (live, abridged)

```json
{
  "success": true,
  "status": "degraded",
  "has_data": true,
  "window_days": 28,
  "window_start": "2026-09-24",
  "window_end": "2026-10-21",
  "model_version": "1",
  "freshness": {
    "status": "degraded",
    "latest_actual_date": "2016-04-24",
    "latest_forecast_date": "2026-11-19",
    "days_since_actual": 3832,
    "actual_count": 1200,
    "forecast_count": 665,
    "coverage": [ { "store_id": "CA_1", "latest_date": "2016-04-24", "days": 120 } ]
  },
  "accuracy": {
    "has_data": false,
    "current": { "sample_size": 0, "wape": null, "mae": null, "rmse": null, "bias": null, "mape": null, "interval_coverage": null, "r2": null },
    "series": [],
    "no_data_message": "Monitoring will populate after forecasts and actual outcomes are recorded."
  },
  "drift_summary": { "evaluated_features": 3, "drifting_features": 2, "warning_features": 0 },
  "alert_summary": { "open": 2, "critical": 1, "warning": 1, "info": 0 },
  "retraining": {
    "recommendation": "investigate",
    "reason": "2 feature(s) have drifted beyond their threshold. Accuracy cannot be measured for the current window, so the drift signal is all there is to go on.",
    "model_version": "1",
    "last_training_date": "2026-10-20T15:36:55.819000Z",
    "drift_features": 2
  },
  "can_manage_alerts": true,
  "can_view_detail": true
}
```

Three contracts to notice in that real payload:

- **`has_data: false` is never a fake chart.** The accuracy block returns nulls
  and the exact message *"Monitoring will populate after forecasts and actual
  outcomes are recorded."* The M5 dataset's actuals end **2016-04-24**, so a
  rolling 28-day window around today genuinely has no scorable pairs.
- **`recommendation: "investigate"` rather than `"no_data"`.** Drift exists even
  though the window is unscorable; reporting "no data" would make a drifting
  model look healthy.
- **`can_manage_alerts` / `can_view_detail`** are derived from the caller's
  permissions, so a client renders the right controls without guessing.

### `GET /v1/monitoring/accuracy`

Returns `current`, `previous`, a `trend` per metric, and a per-window `series`.
**WAPE is the headline metric**: MAPE explodes on this dataset's zero-sales
days, so it is reported but masked to non-zero actuals. Also MAE, RMSE, signed
`bias` (so a systematic over-forecast is visible), `interval_coverage`, and
`r2`.

### `GET /v1/monitoring/drift` (live)

```json
{
  "success": true,
  "has_data": true,
  "model_version": "1",
  "features": [
    { "feature_name": "actual_sales",   "drift_method": "psi", "drift_score": 0.0572, "threshold": 0.25, "status": "stable",         "reference_size": 300, "current_size": 300, "evaluation_date": "2016-04-24" },
    { "feature_name": "forecast_error", "drift_method": "psi", "drift_score": 2.5929, "threshold": 0.25, "status": "drift_detected", "reference_size": 300, "current_size": 300, "evaluation_date": "2016-04-24" },
    { "feature_name": "predicted_sales","drift_method": "psi", "drift_score": 6.4439, "threshold": 0.25, "status": "drift_detected", "reference_size": 300, "current_size": 300, "evaluation_date": "2016-04-24" }
  ],
  "drifting_features": 2,
  "warning_features": 0
}
```

PSI thresholds 0.10 (warning) / 0.25 (drift); KS 0.10 / 0.20; no verdict below
`MIN_DRIFT_SAMPLE = 20`. What is measured is **prediction** drift, not
input-feature drift — feature drift would require persisting the per-request
feature matrix, and the response does not claim otherwise.

### `GET /v1/monitoring/alerts` (live)

```json
{
  "success": true,
  "alerts": [
    {
      "id": "c125012b-f793-4f2b-9a51-03ebf505403a",
      "alert_type": "interval_coverage",
      "severity": "warning",
      "status": "open",
      "title": "Interval coverage is 55%",
      "message": "The 95% interval contained the actual 55% of the time, so it is narrower than it claims.",
      "recommended_action": "The uncertainty band is understated. Widen the residual estimate or treat the published interval as optimistic.",
      "model_version": "1",
      "occurrence_count": 2,
      "details": { "coverage": 0.55, "confidence_level": 0.95 },
      "created_at": "2026-10-21T10:26:14.372043Z",
      "last_seen_at": "2026-10-21T10:26:49.926903Z",
      "acknowledged_at": null,
      "resolved_at": null
    },
    {
      "id": "0a4960aa-d14e-4da1-b389-8b1db12bbf4b",
      "alert_type": "stale_data",
      "severity": "critical",
      "status": "open",
      "title": "No actuals for 3832 days",
      "message": "The most recent actual is 2016-04-24. Accuracy cannot be measured without outcomes.",
      "recommended_action": "Check the ingestion that loads actuals. Every monitoring figure is stale until it resumes.",
      "occurrence_count": 2,
      "details": { "days_since_actual": 3832 }
    }
  ],
  "total": 2, "open_count": 2, "critical_count": 1, "warning_count": 1
}
```

`occurrence_count: 2` instead of two separate rows is the fingerprint
deduplication working — the fingerprint deliberately **excludes the measured
value**, so a metric that keeps breaching updates one alert rather than
creating a new one daily.

That first alert is the platform reporting its own weakness: the published 95%
interval covered the actual 55% of the time.

### `POST /v1/monitoring/evaluate`

Runs ingestion → scoring → drift → alerting and returns what it found. This is
the endpoint the Airflow DAG `sales_forecast_monitoring` calls **@daily** as a
service account; the UI can trigger the same work.

---

## Model registry (`/v1/models`)

| Method | Path | Permission |
|---|---|---|
| GET | `/v1/models` | `view_model_registry` |
| GET | `/v1/models/compare` | `view_model_registry` |
| GET | `/v1/models/{model_version}` | `view_model_registry_detail` |
| GET | `/v1/models/{model_version}/metrics` | `view_model_performance` |
| GET | `/v1/models/{model_version}/lineage` | `view_model_registry_detail` |
| GET | `/v1/models/{model_version}/artifacts` | `view_model_registry_detail` |
| POST | `/v1/models/{model_version}/promote` | `promote_model` |
| POST | `/v1/models/{model_version}/archive` | `archive_model` |
| POST | `/v1/models/{model_version}/rollback` | `rollback_model` |

### `GET /v1/models` (live, abridged)

```json
{
  "success": true,
  "models": [
    {
      "model": "ensemble",
      "label": "Ensemble",
      "registered_model_name": "sales_forecast_models_ensemble",
      "version": "1",
      "stage": "Production",
      "run_id": "67a0ca0922af4ea289f76a9a943ecafd",
      "is_production": true,
      "is_default": true,
      "metrics": { "rmse": 98.0759, "mae": 29.6355, "r2": 0.9930, "mape": 202.0379, "wape": 1.0529, "bias": 6.7479 },
      "training_date": "2026-10-20T15:36:55.819000Z",
      "supported_stores": ["CA_1", "CA_2", "CA_3", "CA_4", "TX_1", "TX_2", "TX_3", "WI_1", "WI_2", "WI_3"],
      "feature_count": 51,
      "data_source": "kaggle_m5",
      "dataset_version": "v1.0.0"
    },
    {
      "model": "xgboost",
      "version": "1",
      "stage": "Production",
      "metrics": { "rmse": 82.9560, "mae": 31.5741, "r2": 0.9950, "wape": 1.1218, "bias": 3.6695 }
    }
  ]
}
```

Those are **training** metrics as published by MLflow. The out-of-sample
backtest measured WAPE **18.2%** and R² **0.458** for the same model — the gap
is target leakage in the feature set, and `/v1/monitoring/accuracy` is the
number to trust.

`/artifacts` returns artifact **metadata only** — names, sizes, types. No bytes
and no object-store paths ever cross the API boundary.

### Mutations

```json
POST /v1/models/sales_forecast_models_ensemble:2/promote
{ "reason": "Backtest WAPE improved from 18.2% to 14.1%", "confirm": true }
```

Every mutation runs **confirm → apply → verify → audit → re-read**: the stage
transition is applied through MLflow's REST API, then the registry is
**re-read to verify it took effect**, then an audit row is written, then the
fresh state is returned. A transition that reported success without being
verified is what previously left the registry with no Production version at
all. If verification fails, the response is `502
REGISTRY_OPERATION_FAILED` — not a success.

`GET /v1/models/compare?base=…&candidate=…` returns direction-aware deltas:
lower-is-better for error metrics, higher-is-better for R², so "improved"
means improved.

---

## Administration (`/v1/admin`)

Every route below requires `manage_users`, **declared on the router** so a new
endpoint cannot be added without it — except the audit log, which requires
`view_audit_logs`.

| Method | Path | Permission |
|---|---|---|
| GET | `/v1/admin/users` | `manage_users` |
| POST | `/v1/admin/users` | `manage_users` |
| GET | `/v1/admin/users/{user_id}` | `manage_users` |
| PATCH | `/v1/admin/users/{user_id}` | `manage_users` |
| POST | `/v1/admin/users/{user_id}/deactivate` | `manage_users` |
| POST | `/v1/admin/users/{user_id}/activate` | `manage_users` |
| POST | `/v1/admin/users/{user_id}/reset-password` | `manage_users` |
| GET | `/v1/admin/permissions` | `manage_users` |
| GET | `/v1/admin/audit-logs` | `view_audit_logs` |

### `POST /v1/admin/users`

```json
{ "email": "analyst@example.com", "full_name": "Dana Okafor", "role": "ANALYST" }
```

Omit `password` and a strong one is generated and returned **once** in the
creation response. It is never stored in plaintext and never returned again —
the only recovery path is `reset-password`.

### Guards

Three invariants the API refuses to break, so the platform cannot be locked out
of:

- you cannot change **your own** role,
- you cannot deactivate **yourself**,
- you cannot remove or deactivate the **last active ADMIN**.

Deactivation takes effect immediately, not at token expiry, because
`get_current_user` re-loads the user from the database on every request.

### `GET /v1/admin/audit-logs`

Append-only. Filterable by `actor_email`, `action`, `resource_type`,
`resource_id`, `outcome`, and a date range, with pagination. Includes
`outcome=denied` rows — a `403` is recorded, so attempts are visible and not
only successes. Passwords and tokens never appear in `details`.

### `GET /v1/admin/permissions`

The live role × permission matrix, 5 roles × 18 permissions. The `/settings`
page renders it, which is why the documented matrix and the enforced rules
cannot drift.

---

## Health

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | open | dependency status and which model version is serving |
| GET | `/health/ready` | open | readiness probe |
| GET | `/metrics` | open | Prometheus exposition |

`/health` reports reachability of MLflow, the object store, and the database.
It deliberately reports **status, not configuration** — no URI, bucket name, or
credential appears in the payload.

---

## Permission index

| Permission | Endpoints |
|---|---|
| `view_forecast` | the `/forecast` page's read surface |
| `generate_forecast` | `POST /v1/forecast` |
| `export_forecast` | CSV download |
| `view_scenario` | `GET /v1/scenario/capabilities`, `/baseline/{id}`, `/{id}` |
| `run_scenario` | `POST /v1/scenario/compare`, `/{id}/rollback` |
| `view_model_performance` | `GET /v1/models/{v}/metrics` |
| `view_model_registry` | `GET /v1/models`, `/compare` |
| `view_model_registry_detail` | `GET /v1/models/{v}`, `/lineage`, `/artifacts` |
| `promote_model` | `POST /v1/models/{v}/promote` |
| `archive_model` | `POST /v1/models/{v}/archive` |
| `rollback_model` | `POST /v1/models/{v}/rollback` |
| `trigger_retraining` | retraining trigger |
| `view_monitoring` | `GET /v1/monitoring/summary`, `/accuracy`, `/data-freshness`, `/alerts` |
| `view_monitoring_detail` | `GET /v1/monitoring/drift` |
| `manage_alerts` | `POST /v1/monitoring/alerts/{id}/acknowledge`, `/resolve`, `POST /v1/monitoring/evaluate` |
| `manage_users` | all of `/v1/admin/users*`, `GET /v1/admin/permissions` |
| `view_audit_logs` | `GET /v1/admin/audit-logs`, `GET /v1/scenario/{id}/audit` |
| `view_all_audit_logs` | unrestricted audit visibility (ADMIN only) |
