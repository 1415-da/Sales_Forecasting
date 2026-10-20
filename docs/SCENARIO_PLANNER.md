# Scenario Planner

Predictive what-if analysis for retail sales forecasts.

> **This is not causal inference.** A scenario compares two model predictions —
> one with your assumptions applied, one without. It tells you what the model
> associates with those conditions, based on patterns in historical data. It
> does not establish that the scenario *causes* the difference. See
> [Limitations](#limitations).

---

## Purpose

A planner wants to ask "what if we ran a 7-day promotion next month?" and get a
defensible number rather than a guess. The Scenario Planner answers by:

1. producing a **baseline** forecast for the chosen store and horizon,
2. applying the planner's **assumptions** to the forecast-time feature matrix,
3. re-running the **same model** on the modified matrix,
4. reporting the **difference**, per day and in total, with uncertainty.

Because both sides come from one model and one feature pipeline, the only
difference between them is the overrides. That is what makes the comparison
meaningful even though the absolute level may be imprecise.

---

## User workflow

| Step | What the planner does | What the system does |
|---|---|---|
| 1 | Picks a store, horizon (7–90d), model version, confidence band (80% / 95%) | Loads capabilities for that model version |
| 2 | Picks a scenario type | Disables types the model cannot express |
| 3 | Sets assumptions | Validates locally against the same rules the API enforces |
| 4 | Clicks **Run scenario** | Claims an `Idempotency-Key`, calls `POST /v1/scenario/compare` |
| 5 | Reads KPIs, chart, table, calendar, narrative | Renders the comparison plus its limitations |
| 6 | Edits an assumption | Marks the result **stale**; button becomes **Update scenario**; previous result stays visible |
| 7 | Optionally withdraws it | **Rollback** with a required reason; audit history preserved |

Changing the **store, model, or horizon** clears the result (a different
question). Changing **assumptions** only marks it stale (same question, new
answer pending).

---

## Architecture

```
┌─ BROWSER ─ Next.js ────────────────────────────────────────────────────────┐
│  app/scenario-planner/page.tsx                                            │
│    left column:  context · type · assumptions · model support · run       │
│    right column: KPIs · chart · table · calendar · insight · limitations   │
│  hooks/  use-scenario-form · -capabilities · -forecast · -rollback         │
│  lib/    scenario-api (Zod-validated) · errors (code → copy) · format      │
└───────────────────────────┬───────────────────────────────────────────────┘
                            │ X-Request-ID · Idempotency-Key · 60s deadline
                            ▼
┌─ FastAPI ─ api/ ──────────────────────────────────────────────────────────┐
│  middleware/   RequestIdMiddleware · error boundary (one envelope)         │
│  routers/      /v1/scenario/{capabilities,compare,{id},rollback,audit}     │
│  observability/ metrics · structured logs · health probes                  │
│                                                                           │
│  services/                                                                │
│    scenario_validation ──► business rules (history, dates, quantiles)     │
│    scenario_capability ──► can this model express this scenario?          │
│    baseline_forecast   ──► generate/reuse baseline  ─┐                    │
│    scenario_feature    ──► apply overrides to future matrix               │
│    scenario_forecast   ──► orchestrate both sides   ─┤ rollback_service   │
│    scenario_impact     ──► per-day + quantile deltas─┘ audit_service      │
│    dataset_lineage     ──► which data trained this model?                 │
│                                                                           │
│  db/  scenario_forecasts · scenario_daily_forecasts · scenario_audit_events│
│       scenario_baseline_forecasts                                         │
└───────┬─────────────────────────┬──────────────────────┬──────────────────┘
        │                         │                      │
        ▼                         ▼                      ▼
   PostgreSQL               MLflow registry          MinIO (S3)
   scenario + audit         model version,           model artifacts,
   tables                   run params (lineage)     feature_schema.json
                                   ▲
                                   │ written by
                            Airflow training DAG
```

### Request pipeline

```
POST /v1/scenario/compare
  │
  ├─ Pydantic validation ................... 422 on failure
  ├─ Idempotency replay check .............. returns original result, or 409 on conflict
  ├─ audit: received
  ├─ Capability check ...................... 409 if the model lacks the features
  ├─ Baseline: generate or reuse ........... 404 / 409 / 503 on model problems
  ├─ Apply overrides ....................... 409 if nothing can be applied
  ├─ Predict baseline + scenario matrices .. 500 on prediction failure
  ├─ Compute impact ........................ 500 on misalignment
  ├─ audit: completed (+ stored response)
  └─ 200 ScenarioComparisonResponse
```

Any failure after the audit row exists moves it to a terminal failure status via
`ScenarioRollbackService`; a **completed baseline is never invalidated** by a
scenario failure.

---

## API contract

Base path `/v1/scenario`. All responses are JSON. Every request accepts
`X-Request-ID` (echoed back); `POST /compare` also accepts `Idempotency-Key`.

### `GET /capabilities?model_version=latest`

Which scenario types the model can express, and why not.

```json
{
  "model_version": "14",
  "feature_count": 9,
  "features": ["lag_1", "is_promotion", "customer_traffic", "price"],
  "capabilities": [
    {
      "scenario_type": "promotion",
      "supported": true,
      "required_features": ["is_promotion", "promotion_intensity", "discount_depth"],
      "available_features": ["is_promotion"],
      "missing_features": [],
      "requirement_mode": "any",
      "reason": null
    }
  ],
  "min_history_rows": 7,
  "max_horizon_days": 90,
  "default_confidence_levels": [0.1, 0.5, 0.9]
}
```

`404` unknown model · `409` model has no feature schema · `503` MLflow down.

### `POST /compare`

```json
{
  "store_id": "CA_1",
  "model_version": "latest",
  "horizon_days": 14,
  "confidence_levels": [0.1, 0.5, 0.9],
  "history": [{ "date": "2026-01-01", "sales": 512.0 }],
  "scenario": {
    "scenario_type": "promotion",
    "start_date": "2026-02-01",
    "end_date": "2026-02-07",
    "promotion_intensity": "medium",
    "discount_depth": 0.25
  },
  "baseline_forecast_id": null
}
```

Returns `ScenarioComparisonResponse`: `scenario_id`, `baseline_forecast_id`,
`scenario_forecast_id`, `model_version`, `model_stage`, `scenario_type`,
`forecast_type` (always `predictive_what_if`), `baseline`, `scenario`, `impact`,
`daily_forecasts`, `applied_overrides`, `unsupported_overrides`,
`modified_features`, `modified_dates`, `limitations`, `dataset_lineage`,
`generated_at`.

Requirements: **≥ 7 days of history**, unique and ascending dates, horizon
1–90, confidence levels unique/ascending and strictly within (0, 1).

### `GET /{scenario_id}` · `GET /baseline/{id}` · `GET /{scenario_id}/audit`

Stored comparison (`410` once its baseline expires), baseline metadata, and the
audit trail. **The audit endpoint has no authorisation yet** — see
[Limitations](#limitations).

### `POST /{scenario_id}/rollback`

Body `{"reason": "…"}` (≥ 3 chars). Returns a `RollbackResponse`.

---

## Error codes

Every failure uses one envelope:

```json
{
  "success": false,
  "error": {
    "code": "MODEL_NOT_FOUND",
    "message": "Requested model version was not found.",
    "field": "model_version",
    "details": {},
    "request_id": "req_8f2c…",
    "timestamp": "2026-10-19T22:46:00Z"
  }
}
```

| Code | HTTP | Meaning | Retry? |
|---|---|---|---|
| `SCENARIO_VALIDATION_ERROR` | 422 | Request violates a business rule | No |
| `SCENARIO_NOT_SUPPORTED` | 409 | Model cannot express this scenario | No |
| `FEATURE_SCHEMA_MISSING` | 409 | Model version has no `feature_schema.json` | No |
| `FEATURE_NOT_AVAILABLE` | 422 | Requested feature absent from the schema | No |
| `MODEL_NOT_FOUND` | 404 | Unknown model version | No |
| `MODEL_NOT_IN_PRODUCTION` | 409 | Model exists but is not promoted | No |
| `MLFLOW_UNAVAILABLE` | 503 | Registry unreachable | **Auto** |
| `ARTIFACT_DOWNLOAD_FAILED` | 502 | Artifact present but unusable | **Auto** |
| `PREDICTION_FAILED` | 500 | Inference failed | Manual |
| `FORECAST_ALIGNMENT_ERROR` | 500 | The two series do not cover identical dates | Manual |
| `DATABASE_UNAVAILABLE` | 503 | Scenario datastore unreachable | **Auto** |
| `BASELINE_NOT_FOUND` | 404 | Referenced baseline missing | No |
| `BASELINE_EXPIRED` | 409 | Baseline past its TTL | No |
| `SCENARIO_NOT_FOUND` | 404 | Unknown scenario id | No |
| `SCENARIO_EXPIRED` | 410 | Stored comparison no longer reproducible | No |
| `SCENARIO_ALREADY_RUNNING` | 409 | Duplicate scenario in flight | No |
| `IDEMPOTENCY_KEY_CONFLICT` | 409 | Key reused with a different payload | No |
| `INTERNAL_SCENARIO_ERROR` | 500 | Unexpected failure | Manual |

Client-side only: `NETWORK_ERROR`, `TIMEOUT` (60s deadline), `INVALID_RESPONSE`,
`REQUEST_ABORTED`.

**Automatic retry** covers only network, timeout, 502, and 503 — at most 2
attempts with exponential backoff (1s, 2s). A 422/404/409 is deterministic and
is never retried. Because one `Idempotency-Key` is reused across a submission's
retries, a retry replays the original result rather than paying for another
inference.

`INTERNAL_SCENARIO_ERROR` responses carry a generic message and empty
`details`: no stack traces, filesystem paths, connection strings, object-store
keys, or internal hostnames ever reach a client.

---

## Rollback behaviour

Rollback is a **status transition, never a delete**.

| Preserved | Changed |
|---|---|
| Audit events (a new `rolled_back` event is appended) | Scenario status → `rolled_back` |
| The baseline forecast and its checkpoint | Export disabled, further runs blocked |
| The stored comparison payload | A badge and banner mark it withdrawn |

A reason of ≥ 3 characters is **required**, because it is written to the audit
trail — a withdrawal with no stated cause is a gap in the record.

Failure handling follows the same principle. Each stage has its own rollback:

| Failure stage | Baseline | Scenario | Audit status |
|---|---|---|---|
| Validation | untouched | not created | `validation_failed` |
| Capability check | untouched | not created | `capability_check_failed` |
| Baseline creation | marked `failed` | not created | `baseline_failed` |
| Feature preparation | **preserved if completed** | failed | `scenario_failed` |
| Prediction | **preserved if completed** | failed | `scenario_failed` |
| Persistence after prediction | preserved | failed, partial results discarded | `scenario_failed` |
| Cancellation / timeout | preserved | cancelled, locks released | `scenario_cancelled` |

A failure handler never raises: losing the audit row is bad, but masking the
original error with a bookkeeping error is worse.

---

## Data lineage

Every comparison can carry the lineage of the data its model was trained on,
read from what the training pipeline already records — never hardcoded:

1. **Training run metadata** (preferred): the MLflow params
   `DatasetLineage.to_mlflow_params()` logs (`data.source`, `data.version`,
   `data.hash`, `data.raw_path`, `data.date_min`, `data.date_max`,
   `data.schema_version`).
2. **Dataset registry** (fallback): the `dataset_manifest.json` each data
   provider writes beside its parquet.

| Field | Source |
|---|---|
| `data_source` | `data.source` / manifest `data_source` |
| `dataset_version` | `data.version` / `dataset_version` |
| `dataset_hash` | `data.hash` — content hash, the key to reproducibility |
| `raw_data_path` | `data.raw_path` |
| `processed_data_path` | `data.processed_path` — **not logged today** |
| `train_start_date` / `train_end_date` | `data.date_min` / `data.date_max` |
| `feature_schema_version` | `data.schema_version` |
| `ingestion_run_id` | **not logged today** |

`lineage_status` is `available` (source, version, and hash all resolved),
`partial`, or `unavailable`. When unavailable the response carries
*"Dataset lineage metadata is unavailable for this model version."* and the
scenario still runs — lineage is advisory, never load-bearing.

Because training does not yet log `processed_data_path` or `ingestion_run_id`,
lineage against today's runs resolves **7 of 9 fields** and reports `partial`.

---

## Observability

**Structured logs** (`scenario` logger, one JSON line per event). Events:
`request_received`, `validation_completed`, `capability_check_completed`,
`baseline_created` / `baseline_reused`, `overrides_applied`,
`prediction_completed`, `impact_calculated`, `completed`, `failed`,
`rolled_back`. Every line carries `request_id`, `store_id`, `model_version`,
`scenario_type`, `status`, and — where applicable — `scenario_id`,
`duration_ms`, `error_code`.

Request payloads are **never** logged: only `history_rows`. All strings pass
through the same redaction as error responses.

**Metrics** at `GET /metrics`, in Prometheus text format with no client library:

| Metric | Type | Labels |
|---|---|---|
| `scenario_requests_total` | counter | `scenario_type`, `status` |
| `scenario_requests_failed_total` | counter | `scenario_type`, `error_code` |
| `scenario_duration_seconds` | histogram | `scenario_type` |
| `scenario_rollback_total` | counter | `outcome` |
| `baseline_reuse_total` | counter | `reused` |
| `scenario_prediction_failures_total` | counter | `error_code` |
| `scenario_capability_checks_total` | counter | `scenario_type`, `outcome` |

`store_id` is deliberately **not** a label — thousands of stores would multiply
every series. Per-store detail lives in `scenario_audit_events`.

**Health**: `GET /health` reports api, database, MLflow, MinIO, and
feature-schema state and stays **200** while the process serves (an upstream
outage must not trigger a restart). `GET /health/ready` returns **503** when a
critical dependency is down. `GET /health?deep=false` touches nothing.

---

## Limitations

### Model and method

- **Not causal.** The narrative is phrased associatively ("is associated with")
  by construction, and a test asserts no causal phrasing appears.
- **Uncertainty is model error only.** Intervals reflect quantile or
  residual spread, not external shocks, competitor action, or supply issues.
- **Assumptions are yours.** The model does not judge whether a 25% discount is
  realistic; it reports what it associates with those feature values.
- **Partial scenarios are possible.** If a model lacks some levers, the run
  proceeds with what it has and flags the rest — treat the impact as a lower
  bound on what you described.

### Known gaps in this build

| Gap | Effect |
|---|---|
| `FutureFeatureBuilder` / `FeatureMatrixPredictor` not implemented | `POST /compare` returns `PREDICTION_FAILED` with *"Scenario inference is not configured"* |
| Training does not write `feature_schema.json` | Capability detection returns `FEATURE_SCHEMA_MISSING` |
| Registry promotion silently fails in the DAG | No Production version exists; `BASELINE_REQUIRE_PRODUCTION_STAGE=false` is needed locally |
| **Target leakage in training** (`quantity_sold == sales`) | Scenario deltas are not yet trustworthy regardless of this layer's correctness |
| No store-directory or history endpoint | The UI's store list is a placeholder; history must be supplied by the caller |
| Audit endpoint unauthenticated | Do not expose `/v1/scenario/{id}/audit` to untrusted networks |
| Locks and idempotency are per-process | Duplicate scenarios can slip through with multiple workers |
| No Alembic migrations | Schema is created with `create_all`; added columns need manual DDL on an existing database |
| Rollback not reflected in `GET /{id}` | A refreshed page can show a rolled-back scenario as live |

---

## Future causal-upgrade path

Today's answer is *associational*. Making it causal is a modelling problem, not
a UI one, and the layers here are deliberately shaped to allow it:

1. **Fix the leakage first.** With `quantity_sold == sales` in the feature set,
   no causal claim is possible. Remove target-derived features, shift rolling
   windows, and re-establish an honest baseline.
2. **Separate controllable from observed features.** A causal estimate needs
   interventions to be variables you can actually set (price, promotion,
   staffing), not consequences of sales.
3. **Capture natural experiments.** Persist forecast-vs-actual outcomes for
   every scenario run (`scenario_forecasts` already stores the predictions) so
   realised effects can be compared to predicted ones.
4. **Estimate uplift from historical interventions.** Difference-in-differences
   across comparable stores, or a synthetic-control baseline, replaces the
   "re-run the model with a flag flipped" approach.
5. **Add an explicit causal mode.** `forecast_type` is already an enum with one
   value; `causal_estimate` becomes a second, carrying its own identification
   assumptions and confidence semantics, so the UI can label the two differently
   rather than silently changing meaning.
6. **Validate with holdout interventions.** A promotion calendar withheld from
   training provides ground truth for calibration.

Until then, `forecast_type` stays `predictive_what_if` and every response
carries its limitations.
