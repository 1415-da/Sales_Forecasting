# Forecast workflow

How a sales history becomes a forecast, and what the result does and does not
mean. Read [Limitations](#limitations) before acting on any number this
produces.

---

## 1. The user's path

```
Browser (http://localhost:3001/forecast)
   │
   │  GET /health      → API state + the serving Production model
   │  GET /v1/models   → which version serves each model, with its metrics
   ▼
Forecast Console
   │  store · horizon · model · version · confidence
   │  history: demo | CSV upload | manual entry
   │
   │  POST /v1/forecast
   ▼
FastAPI
   │  1. resolve the version          model_registry_service
   │  2. load model + preprocessing   artifact_service
   │  3. validate history vs contract feature_service
   │  4. recursive projection         forecast_service
   │  5. uncertainty band             forecast_service
   ▼
Response → KPI cards · chart + band · sortable table · CSV export
```

Nothing in the browser talks to MLflow, MinIO, or PostgreSQL. The frontend
receives exactly one piece of backend configuration, `NEXT_PUBLIC_API_URL`, and
it carries no credential.

## 2. Supplying history

Three routes, one set of rules. All three produce the same array of
`{date, sales}` rows, validated by `validateHistory` in
`frontend/lib/sample-history.ts`, and the API re-validates server-side.

| Mode | What it is |
|---|---|
| **Demo data** | 60 days generated from a fixed seed, with weekly seasonality and a mild trend, ending yesterday. Labelled as demo in the UI. Reproducible: the same forecast every time. |
| **CSV upload** | Requires `date` and `sales`. Also reads `store_id`, `quantity_sold`, `profit`, `customer_traffic` when present. Accepts the aliases `day`/`ds`/`timestamp` and `revenue`/`y`/`amount`, quoted fields, CRLF, and US slash dates. Unreadable rows are skipped **and reported**, never dropped silently. |
| **Manual entry** | A 30-day dated template; the user fills in sales. Days can be appended. |

### Validation rules

| Rule | Severity | Why |
|---|---|---|
| ≥ 30 days (`FORECAST_MIN_HISTORY_DAYS`) | error | the models use lags and rolling windows up to 30 days |
| 30–59 days | warning | the 30-day rolling features come from a partial window |
| no duplicate dates | error | lag features are positional, so a repeat shifts every window |
| ordered oldest-first | error (API) | same reason; sorting silently would hide a data problem |
| at least one positive sales day | error | there is no level to project from |
| no missing calendar days | warning | gaps shift the positional windows |
| store known to the model's encoder | error | substituting a different store would return a confident forecast for the wrong one |

## 3. The feature contract

The current Production models consume **51 features**, read at request time
from the training run's `feature_cols.pkl` rather than hardcoded:

| Family | Count | Built from |
|---|---|---|
| calendar | 7 | `year`, `month`, `day`, `dayofweek`, `quarter`, `weekofyear`, `is_weekend` |
| cyclical | 6 | sin/cos of month, day, day-of-week |
| holiday | 1 | `is_holiday`, from the US public-holiday calendar |
| lags | 7 | `sales_lag_{1,2,3,7,14,21,30}` |
| rolling | 25 | `{3,7,14,21,30}` × `{mean,std,min,max,median}` |
| store | 1 | `store_id`, label-encoded with the training encoder |
| same-day measures | 4 | `quantity_sold`, `profit`, `customer_traffic`, `has_promotion` |

Features are then standardised with the training `StandardScaler`, because the
models were fitted on scaled inputs. The target was never scaled, so
predictions come back in sales units directly.

A model whose feature list contains a family this builder cannot reproduce is
rejected with `FEATURE_MISMATCH` rather than served with zero-filled inputs.

### Reconstructing inputs that cannot be known

Four model inputs describe the forecast day itself, so no caller can supply
them for a future date. They are **reconstructed from the training
relationship, not guessed**:

| Input | Policy | Why |
|---|---|---|
| `quantity_sold` | previous day's sales × the ratio observed in the request (default 1.0) | `quantity_sold == sales` holds exactly in the training frame: both are summed from the same per-product quantity |
| `profit` | previous day's sales × the observed ratio (default `FORECAST_PROFIT_TO_SALES_RATIO`, 0.3) | `profit == 0.3 × sales` holds exactly, from the M5 ingestion (`profit = 0.3 × revenue`) |
| `customer_traffic` | mean of the last 28 supplied days, else `0.0` | no source file supplied it, so it was `0` throughout training |
| `has_promotion` | mean of the last 28 supplied days, else `0.0` | ditto |
| `is_holiday` | the US public-holiday calendar | the training pipeline overwrites the merged column with `holidays.US()`, so inference reproduces the same source |

A request that supplies `profit` or `quantity_sold` has its own ratio used
instead of the default, computed pairwise over the window (total ÷ total)
rather than as a mean of per-day ratios.

Every reconstructed input is named in `metadata.imputed_features`.

**This replaced a real defect.** The previous Streamlit path substituted the
constants `quantity_sold = 100`, `profit = 1000`, `customer_traffic = 500` —
values far outside the training distribution — and then multiplied its output
by a ratio clipped to `[0.6, 2.0]` to pull the result back towards recent
history. With the training-consistent policy, all three Production models
reproduce the history level without any correction (measured: history mean
2977 → XGBoost 3058, LightGBM 3085, Ensemble 3067), so **no calibration is
applied**.

## 4. Recursive projection

Each forecast day's lag and rolling inputs are computed from the observed
history extended by the days already predicted. That is what turns a
single-step model into a multi-day path, and it is why error compounds with
horizon.

A prediction that is negative is clamped to zero (sales cannot be negative);
one that is non-finite fails the request with `PREDICTION_FAILED` rather than
putting `NaN` into a response.

## 5. Uncertainty

The trained models are point estimators — no quantile regression, no native
prediction intervals — so the band is **constructed**, and the method is
reported on every response rather than implied.

| `metadata.uncertainty_method` | How |
|---|---|
| `residual_normal` (preferred) | a normal interval using the model's own held-out RMSE, read from the metric the training run logged for that model |
| `history_volatility` (fallback) | residual spread approximated from the standard deviation of the supplied history, used when the run logged no RMSE. It describes the **data**, not the model's accuracy, and says so in `warnings` |

Both are widened by `sqrt(step)`: in a recursive forecast each day inherits the
error of the days before it, so variance accumulates roughly linearly in the
number of steps.

The spread is bounded to `[2%, 50%]` of the forecast level. A model reporting a
near-zero RMSE would otherwise produce a band of visually zero width, which
would present a recursive forecast as certain.

Interval half-width for day *n*:

```
z(confidence) × residual_std × sqrt(n)
z(0.80) = 1.2816      z(0.95) = 1.9600
```

## 6. API contract

### `GET /v1/models`

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
      "metrics": {"rmse": 98.08, "mae": 29.64, "r2": 0.993},
      "training_date": "2026-10-20T15:26:02Z",
      "supported_stores": ["CA_1", "CA_2", "CA_3", "CA_4", "TX_1", "TX_2", "TX_3", "WI_1", "WI_2", "WI_3"],
      "feature_count": 51,
      "data_source": "kaggle_m5",
      "dataset_version": "v1.0.0"
    }
  ],
  "default_model": "ensemble",
  "production_available": true,
  "supported_stores": ["CA_1", "..."],
  "warnings": []
}
```

An empty registry returns **200** with `models: []`, `production_available:
false`, and a warning naming the DAG to run. "Nothing has been trained yet" is
a state the UI renders, not an error worth retrying.

### `POST /v1/forecast`

Request fields: `store_id`, `model` (`ensemble`|`xgboost`|`lightgbm`),
`model_version` (a registry number or `latest`), `horizon_days` (1–90),
`confidence_level` (`0.8`|`0.95`), `history` (oldest-first).

The response shape is in the [README](../README.md#post-v1forecast).

### Errors

One envelope for every failure:

```json
{
  "success": false,
  "error": {
    "code": "INSUFFICIENT_HISTORY",
    "message": "At least 30 days of history are required; 10 were supplied.",
    "field": "history",
    "details": {"required_days": 30, "supplied_days": 10},
    "request_id": "req_4f1c8a...",
    "timestamp": "2026-10-21T09:30:00Z"
  }
}
```

| Code | Status | Retryable | Meaning |
|---|---|---|---|
| `INSUFFICIENT_HISTORY` | 422 | no | fewer days than the model's windows need |
| `SCENARIO_VALIDATION_ERROR` | 422 | no | an unusable field, e.g. an unknown store |
| `MODEL_NOT_FOUND` | 404 | no | unknown model or version |
| `MODEL_NOT_IN_PRODUCTION` | 409 | no | the version exists but is not promoted |
| `FEATURE_MISMATCH` | 409 | no | the model needs inputs a sales history cannot provide |
| `ARTIFACT_DOWNLOAD_FAILED` | 502 | yes | the artifact could not be retrieved |
| `PREDICTION_FAILED` | 500 | no | the model failed during inference |
| `MLFLOW_UNAVAILABLE` | 503 | yes | the registry could not be reached |

The frontend retries only transport failures and 502/503/504, at most twice
with backoff. A 404 or 422 will not fix itself, and retrying it only delays the
error the user needs to see.

No stack trace, filesystem path, connection string, object-store key, or
internal hostname ever reaches a response.

## 7. Two implementation notes worth knowing

**Artifacts are fetched over the MLflow artifact proxy**, not through the
client's artifact repository. Two reasons: the API then needs no object-store
credential at request time (the tracking server proxies MinIO), and it is
immune to the client/server version split described next. Downloads are cached
on disk in `MODEL_CACHE_DIR` (a Docker volume), and loaded bundles are cached
in memory with an LRU of `MODEL_CACHE_SIZE`.

**The registry is never enumerated with `search_registered_models()`.** The
deployed tracking server is MLflow 2.9.2 while an installed client may be 3.x.
In that pairing `search_registered_models()` returns an **empty list** and
`list_artifacts()` on a sub-path raises 404 against the removed
`/logged-models` endpoints — both silently. Code relying on either would report
"no models available" while three Production versions exist. Enumeration is
therefore driven by `FORECAST_MODELS` and uses only the calls verified to work
across the split: `search_model_versions(filter_string=...)`,
`get_model_version`, and `get_run`.

The serving layer also loads the **raw joblib artifacts**
(`models/<name>/<name>_model.pkl` plus `scalers.pkl`, `encoders.pkl`,
`feature_cols.pkl`) rather than the flavoured `mlflow_model/<name>` directories.
Those were written by MLflow 3.x: the LightGBM flavour is serialised as
`model.skops` and the descriptors declare `mlflow_version: 3.17.1`. Loading
them would couple the API to skops and to the writer's exact flavour version.
The raw artifacts are the same weights and additionally carry the preprocessing
objects inference needs.

## Limitations

1. **Target leakage in the trained models.** `quantity_sold` in the training
   frame is `sales` under another name, and `profit` is a fixed share of it.
   The models lean heavily on those columns, which is why the reported R² is
   0.995 and WAPE is ~1%: those figures largely measure the identity, not
   forecasting skill. **The headline metrics should not be used to judge
   accuracy.** Dropping the leaked columns and retraining is the highest-value
   next change.

2. **Forecasts behave as a level projection.** Following from (1): with the
   leaked same-day inputs reconstructed from a lag-1 proxy, the output tracks
   the recent sales level and expresses little day-to-day movement. It is
   stated in `warnings` on every response and surfaced in both UIs.

3. **The training window is narrower than the lineage suggests.**
   `max_sales_files: 365` in `include/config/ml_config.yaml` means training used
   2011-01-29 → 2012-01-28 (3,650 store-days) while the dataset lineage
   advertises 5.2 years. Raising it needs chunked aggregation first.

4. **No forecast persistence.** A forecast is computed and returned; nothing is
   stored. `forecast_id` identifies a response, not a retrievable record. There
   is no forecast history view and no accuracy backtest against actuals.

5. **No authentication.** Any client that can reach the API can request a
   forecast. The API is not exposed publicly by the compose setup, but there is
   no authorisation layer to add a public route to.

6. **Model versions beyond the serving one are not listed.** The version
   selector offers *Latest Production* and the serving version. The API accepts
   any registry version number, so a specific one can still be requested
   directly.

7. **Horizon capped at 90 days.** Beyond that a recursive single-step model is
   projecting almost entirely from its own output.

8. **The frontend runs a dev server in Docker.** `frontend` uses `next dev`
   against a bind mount, which is right for development and wrong for
   production. A production image needs `next build` plus `next start`
   (`NEXT_OUTPUT_STANDALONE=true` is wired for it).

9. **No Alembic migrations.** Scenario tables are created with
   `metadata.create_all`. The forecast endpoints use no database.
