# Data & Retraining workspace

Monitoring → **Data & Retraining** lets an Analyst upload historical sales, turn them into an immutable dataset version, and train candidate models on it with the existing Airflow DAG. An ML Engineer or Administrator then approves (or rejects) a candidate before anything serves forecasts.

```
Analyst                                   API (FastAPI)                        Airflow 3 / MLflow / MinIO
──────                                    ─────────────                        ──────────────────────────
upload CSV/Parquet ──POST /v1/training/datasets──▶ raw bytes → MinIO (write-once)
                                          validate → report (draft: validated|invalid)
"Create immutable version" ─POST …/{id}/validate─▶ re-read from MinIO, re-validate,
                                          seal: processed.parquet + manifest.json (hashed)
"Start training" ──POST /v1/training/runs (Idempotency-Key)─▶ row committed, then
                                          POST /api/v2/dags/sales_forecast_training/dagRuns
                                            dag_run_id = training__<request id>
                                            conf = server-resolved manifest + hashes ──▶ DAG loads that exact version,
progress panel ◀──GET /v1/training/runs/{id}── sync from Airflow task states          trains, gates, compares,
                                          collect summary XCom on success ◀──────── registers candidates (stage None)
ML Engineer
"Approve & deploy" ──POST /v1/models/{v}/promote (expected_identity + reason)─▶ gates + smoke forecast
                                          → MLflow stage Production, previous archived, decision recorded
"Generate forecast with this model" ──▶ /forecast?model=&version=&store=&dataset=
```

## Three different kinds of sales data

| Workflow | Where | Stored? | Used for |
|---|---|---|---|
| **Training upload** | Monitoring → Data & Retraining, `POST /v1/training/datasets` | Yes: immutable dataset versions in MinIO + `training_datasets` | Training runs only |
| **Forecast history** | Forecast Console, `POST /v1/forecast` | No | One forecast request |
| **Monitoring actuals** | Monitoring DAG → `actuals` table | Yes | Scoring past forecasts |

Each has its own routes, permissions, validation and records. A training upload never becomes forecast history or monitoring actuals, and vice versa. The Forecast Console can *read* a store's recent days from a sealed training dataset (`GET /v1/training/datasets/{id}/history`), which returns recorded days as they are and generates nothing.

## Ingestion path

Every upload goes through the same connected path:

```
upload (CSV / Parquet / several files / ZIP)
  -> streamed to staging, hashed, originals stored write-once in MinIO
  -> processing job (inline <= 2 MB, otherwise a bounded background worker)
       package inspection   ZIP limits, path/symlink/nested-archive checks, content sniffing
       adapter detection    evidence-based: exact / partial / manual; never a silent guess
       confirmation         currency, policies, mapping (status needs_mapping until answered)
       normalization        adapter -> canonical store_day_v1 frame + retained context
       strict validation    store_day_v1 contract, rows, duplicates, readiness
       append combination   when chosen
       normalized Parquet + context Parquet stored separately (content-addressed)
  -> confirm (seal)         only at the reviewed mapping version, warnings acknowledged
  -> immutable version -> Airflow training request (manifest reference only)
```

Session statuses: `processing`, `needs_mapping`, `invalid` (a data rule failed), `validated`, `ready` (immutable), `failed` (a storage or processing failure; never a verdict on the data; **Retry processing** re-runs it from the stored originals). A restart marks interrupted jobs `failed` so they can be retried. Changing the adapter, mapping or policies bumps `mapping_version` and replaces the earlier validation; sealing an older view is refused.

Detection does not mean acceptance: a recognised source still goes through the strict canonical validator.

## Adapters

| Adapter | Recognises | Maps |
|---|---|---|
| `canonical_store_day` v1 | A file with `date`, `store_id`, `sales` (case- and whitespace-insensitive) | Unchanged. SNAP derived for `CA_/TX_/WI_` stores, 25 December closure derived; extra columns refused (use the generic adapter to retain them). |
| `rossmann` v1 | `train.csv`-like file with Store, Date, Sales plus Rossmann fields; optional `store.csv`; `test.csv` and `sample_submission.csv` excluded | `Store` -> `store_id` as `rossmann_<Store>` (text, never `1.0`; namespace `rossmann`); `Date` -> `date`; `Sales` -> `sales` (revenue); `Open` -> `is_closed = 1 - Open` (0/1 only, blanks are errors, never inferred from zero sales; overrides any closure calendar). SNAP and the US holiday feature are **not** applied. DayOfWeek, Customers, Promo, StateHoliday, SchoolHoliday and store metadata are retained as context, not model inputs, and enable no scenario. Without `Open`, a closure policy must be chosen. |
| `mapped_tabular` v1 | Any single CSV/Parquet table | User maps date, store (or confirms a single named store), target, optional open/closed flag; must confirm target = revenue (quantity is refused), frequency = daily, grain = store-day (transaction/product rows are refused, never aggregated), currency, and a date format when dates are not ISO (ambiguous dates are never guessed). Unmapped columns are retained as context. |

Not every dataset can be used. A recognised but unsupported shape is refused with what would be needed instead.

### store.csv join

One row per Store is required (`STORE_METADATA_DUPLICATES` otherwise). The join is validated many-to-one, and the row count before and after is reported; unmatched stores are listed. The metadata is retained as context only.

## Canonical contract (`store_day_v1`)

| Column | Required | Meaning |
|---|---|---|
| `date` | yes | Calendar day, `YYYY-MM-DD` |
| `store_id` | yes | Letters, digits, `_` or `-`, up to 64 characters |
| `sales` | yes | Total daily revenue of the store, in the confirmed currency |
| `snap_day` | no | 0/1; derivation as per the adapter |
| `is_closed` | no | 0/1; from the source, or a confirmed policy |

Dataset metadata recorded in the manifest:
- `source_namespace`, `target_type` (revenue), `currency` (ISO code or explicitly `UNKNOWN`), `frequency`, `schema_version` and `source_grain`;
- the adapter id and version, and the mapping and its version;
- the feature policies (`snap`, `closure`, `missing_dates`, `holiday_calendar`);
- the transformations applied, and the retained columns.

**Currency.** Any ISO code, or an explicit `UNKNOWN`, can be trained. Only the serving currency (`TRAINING_ALLOWED_CURRENCIES`) can be **deployed** over the current model. The currency is never inferred from a country and never pre-selected.

### Validation stages and issues

The report tags every issue with `code`, `severity`, `stage` (file, detection, mapping, schema, rows, readiness, append, deployment), `file`, `column`, the affected count, examples with source row numbers, a message, and a suggested action. Row counts are `null`, and the UI says "Row validation not performed", when a schema or earlier stage stopped validation.

| Rule | Behaviour |
|---|---|
| Target | numeric and finite; blank -> `NULL_TARGET`; text -> `NON_NUMERIC_TARGET`; negative per `TRAINING_NEGATIVE_SALES_POLICY` |
| Duplicates | identical store-days collapsed (`DUPLICATE_KEYS`, counted); differing -> `CONFLICTING_RECORDS`, checked across the whole file |
| Missing dates | gaps inside a store's history -> `MISSING_DATES`, or the explicit `exclude_stores` policy (`STORES_EXCLUDED`, must be acknowledged before sealing; stores listed) |
| History | each store >= warm-up (longest lag/window in `ml_config.yaml`, 30) + 30 days before the validation window -> `INSUFFICIENT_HISTORY` |
| Evaluation window | >= 140 distinct dates, split by calendar date 70/10/20 -> `INSUFFICIENT_EVALUATION_WINDOW` |
| Deployment preview | `DEPLOYMENT_DOMAIN_MISMATCH` warning when a model trained on the data could not replace the serving model (currency, store coverage); training is still allowed |

## Upload limits

Configured in one place (`include/data_ingestion/limits.py`, overridable by environment) and shown on the upload card. The browser checks them before sending; the API enforces them while streaming (413 with the limit and alternatives); the inspector enforces the ZIP limits.

| Limit | Default | Variable |
|---|---|---|
| Whole upload | 200 MB | `TRAINING_UPLOAD_MAX_BYTES` |
| Any one file (incl. ZIP members) | 150 MB | `TRAINING_UPLOAD_MAX_FILE_BYTES` |
| Files per upload | 10 | `TRAINING_UPLOAD_MAX_FILES` |
| ZIP expanded size | 500 MB | `TRAINING_UPLOAD_MAX_EXPANDED_BYTES` |
| ZIP compression ratio | 100:1 | `TRAINING_UPLOAD_MAX_COMPRESSION_RATIO` |
| Rows after normalization | 5,000,000 | `TRAINING_UPLOAD_MAX_ROWS` |
| Processing time per job | 900 s | `TRAINING_PROCESSING_TIMEOUT_SECONDS` |
| Concurrent jobs | 2 | `TRAINING_MAX_CONCURRENT_JOBS` |
| Processed inside the request | <= 2 MB | `TRAINING_INLINE_PROCESSING_BYTES` |

**Rationale.** The Docker VM has 8 GB of memory, shared by every service. Rossmann's 38 MB, 1.0 M-row `train.csv` parses and validates in about 3 s and occupies under 1 GB while parsed. Columns are read as text and only the needed ones are loaded. Files are streamed to disk and to MinIO, never held whole in the request. Processing runs off the request so a browser or proxy timeout cannot cut it short, and the session can be reloaded at any time.

**ZIPs.** These are refused: path traversal, absolute paths, symlinks, encrypted members, nested archives, executable or serialized members (`.pkl`, `.joblib`, `.py`, ...), nesting more than one folder deep, duplicate file names, and members whose content does not match their extension. OS metadata (`__MACOSX/`, `.DS_Store`, `._*`, `Thumbs.db`) is ignored via an allowlist. Other non-data files are listed as excluded.

## Dataset versions and combination

- **Independent:** train on this file alone. Nothing else is added.
- **Append to compatible history:** combine with a sealed version. This is allowed only when schema, target, frequency, currency and context features match, the **source type is the same** (real and synthetic are never blended), and at least one store is shared (no shared store means a different domain). Overlapping store-days:
  - identical → de-duplicated deterministically (counted);
  - different → **rejected** by default, or replaced by the upload with the explicit `prefer_new` correction policy (counted).
  The base version is never modified.

Sealing writes, once each, under `s3://training-datasets/datasets/<id>/`:
- `source/<sha256>.<ext>`: the original upload;
- `processed/<content sha256>.parquet`: the canonical records;
- `manifest.json`: id, name, version, mode, sources (with hashes), target, currency, frequency, schema, date range, row/store counts, source types, context features, validation summary, policy, creator and timestamps.

Keys are generated from ids and hashes. Nothing a user typed becomes part of a path. A key that already holds different bytes is refused, so a sealed version cannot be overwritten. The content hash is computed over a canonical rendering of the records, so the same data hashes the same whether it arrived as CSV or Parquet.

## Training

Approved configurations (the browser sends only an id):

| Id | What | Who |
|---|---|---|
| `standard_v1` | XGBoost + LightGBM with configured parameters, validation-weighted ensemble (about a minute on ~8k store-days) | Analyst, ML Engineer, Admin |
| `tuned_v1` | Optuna search, 20 trials per model | ML Engineer, Admin |

The conf passed to Airflow is small and resolved by the server from the dataset record: `training_request_id, dataset_id, dataset_version, manifest_uri, manifest_sha256, content_sha256, training_config_id, deployment_policy=manual_review`.

What the DAG does differently for these runs (the scheduled M5 path is unchanged):

1. **extract:** loads the manifest and the Parquet, and checks three hashes (manifest bytes, Parquet object, decoded records). It never calls the data provider, never generates data, and has no file-count limit.
2. **validate:** re-runs the full contract on every record under the policy recorded at sealing.
3. **train:** builds features with the same pipeline, splits by calendar date, fits encoders and scalers on the training window only, chooses ensemble weights on validation, and scores the test window once. Lineage and `training_request_id` are tagged on the MLflow run, and per-row holdout predictions are logged.
4. **register:** registers xgboost, lightgbm and ensemble versions in stage **None**, tagged with the request, the dataset and `deployment_policy=manual_review`. **Production promotion is skipped.**
5. **quality gate:** reloads the artifacts from MLflow (not from memory) and checks:
   - the feature contract (ordered, no same-day target columns);
   - finite predictions of the right shape, identical to training;
   - encoder store support equal to the dataset;
   - the minimum test sample;
   - metrics present.
   If every model fails, the run fails.
6. **compare with production:** scores the current Production model on the candidate's exact test rows. If the champion trained on dates inside that window, cannot be fed the dataset's features, or serves none of its stores, the result is **"Not directly comparable"** and no improvement is claimed.
7. **summarize:** logs `model_bundle.json`, which records:
   - the ordered feature schema, target, frequency and currency;
   - the supported stores and the ensemble's component weights;
   - the preprocessing and uncertainty method;
   - the dataset hashes and library versions.
   It also logs `evaluation/report.json`, and hands the API a summary through XCom.
8. **cleanup:** removes only `/tmp/performance_report.json`. Datasets and artifacts are never touched.

### Submission safety

- A double click or a retry sends the same `Idempotency-Key` and returns the same request. One request maps to exactly one `dag_run_id`.
- The request row is committed **before** Airflow is called.
- If the call times out, the row is marked `submission_uncertain`. The next status read looks up the known run id. If the run never arrived, it is re-submitted under the **same id**; if it did, Airflow refuses it as a duplicate (409).
- If Airflow is unreachable, the row is `pending` and is retried the same way.
- A paused or missing DAG, or bad credentials, are reported before anything is recorded.
- The browser polls with backoff (3s growing to 30s) and stops on terminal states. The selected run is in the URL, so a refresh resumes it.

## Deployment approval

Deployment uses the existing **`POST /v1/models/{version}/promote`**; there is no second deployment API. When the version belongs to a training request (matched on its MLflow run id), the request must also carry:
- `"expected_identity": "<model>:<version>"`, which the UI makes the reviewer type;
- a `reason` of 5 or more characters.

These gates then run at that moment:

| Gate | Blocks when |
|---|---|
| training_succeeded / quality_gate | The run did not succeed or the candidate failed its gate |
| business_domain | Target is not `sales`, frequency is not daily, or the currency is not allowed |
| store_coverage | The candidate does not serve every store the current Production version serves. Scoped (per-store) deployment is not supported, so a different store domain cannot replace the shared model. |
| smoke_forecast | The serving path (registry → artifact download → feature builder → recursion) cannot produce 7 finite days from the candidate dataset's real history |

A blocked attempt is audited (`model.deployment_blocked`) with the check results. On success:
- the version moves to Production and the previous version is **archived**, which keeps it available for rollback;
- the model cache is cleared;
- a `deployment_decisions` row records who, why, the checks, and the replaced version.

Every forecast resolves and reports its exact `model`, `model_version` and `run_id`. In-flight requests keep the bundle they resolved, and saved forecasts are never rewritten. Rejection (`POST /v1/training/runs/{id}/reject`, ML Engineer or Admin) is recorded and changes nothing in the registry.

**Rollback:** Model Registry → the archived version → Roll back. This is the existing audited endpoint.

## Permissions

| | Analyst | ML Engineer | Admin |
|---|---|---|---|
| Upload, validate, seal (`manage_training_data`) | ✓ | ✓ | ✓ |
| Start `standard_v1` (`request_training`) | ✓ | ✓ | ✓ |
| Start `tuned_v1` (`trigger_retraining`) |  | ✓ | ✓ |
| See the Airflow run link |  | ✓ | ✓ |
| Approve & deploy / reject (`promote_model`) |  | ✓ | ✓ |

Viewers and Business Users do not see the tab. There is no per-user store scope in this RBAC model; store scope is a platform policy (`TRAINING_ALLOWED_STORES`).

## Records and audit

- **Tables:** `training_datasets`, `training_requests` and `deployment_decisions` (Alembic revision `8d41c0a7e2f3`; also created at API start-up).
- **Audit actions:**
  - `dataset.uploaded` and `dataset.sealed`;
  - `model.retraining_requested`, `model.promoted`, `model.candidate_rejected` and `model.deployment_blocked`.
- **MLflow:** run tags and model-version tags carry `training_request_id`, `dataset_id` and `dataset_version`.

## Failure recovery

| Situation | What happens / what to do |
|---|---|
| Airflow down when starting | 503; nothing recorded. Start again later. |
| Timeout while starting | `submission_uncertain`; resolved automatically on the next status read. |
| DAG paused | 502 with the reason; unpause `sales_forecast_training` in Airflow. |
| Task failure | Status `failed` with the failing task and attempt; the Airflow link (ML Engineer) has the log. Production is unchanged. |
| All candidates fail the gate | The run fails at `quality_gate_task`; nothing is deployable. |
| Deployment blocked | Production is unchanged; the dialog lists each check. |
| Wrong model deployed | Roll back from the Model Registry. |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TRAINING_DATASET_BUCKET` | `training-datasets` | MinIO bucket for datasets (created on first use) |
| `TRAINING_UPLOAD_*`, `TRAINING_PROCESSING_TIMEOUT_SECONDS`, `TRAINING_MAX_CONCURRENT_JOBS`, `TRAINING_INLINE_PROCESSING_BYTES` | see Upload limits | Upload and processing limits |
| `TRAINING_NEGATIVE_SALES_POLICY` | `reject` | Returns policy |
| `TRAINING_ALLOWED_STORES` | empty (any) | Store scope |
| `TRAINING_ALLOWED_CURRENCIES` | `USD` | Serving currency: candidates in another currency cannot be deployed |
| `AIRFLOW_API_URL` | `http://api-server:8080` | Airflow REST API (Docker service name) |
| `AIRFLOW_API_USERNAME` / `_PASSWORD` | `admin` / `admin` | API's Airflow service credentials (backend only) |
| `AIRFLOW_PUBLIC_URL` | `http://localhost:19382` | Browser link to runs |
| `TRAINING_DAG_ID` | `sales_forecast_training` | DAG to trigger |

The API reaches MinIO with the existing `MLFLOW_S3_ENDPOINT_URL` / `AWS_*` variables, and the Airflow workers use the same ones from the Astro image. The stack keeps running under `astro dev start`. After changing API code, rebuild just the API service as described in [DEPLOYMENT.md](DEPLOYMENT.md). Volumes and existing datasets are never deleted.
