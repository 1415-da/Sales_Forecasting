# Deployment

How to run the platform, configure it, create the first administrator, harden
it, and keep its data.

- [1. Prerequisites](#1-prerequisites)
- [2. Start the stack](#2-start-the-stack)
- [3. Services and ports](#3-services-and-ports)
- [4. Environment variables](#4-environment-variables)
- [5. Initial administrator](#5-initial-administrator)
- [6. Database migrations](#6-database-migrations)
- [7. Train and promote a model](#7-train-and-promote-a-model)
- [8. Security configuration](#8-security-configuration)
- [9. Persistence and backup](#9-persistence-and-backup)
- [10. Verifying a deployment](#10-verifying-a-deployment)
- [11. Troubleshooting](#11-troubleshooting)

---

## 1. Prerequisites

| | |
|---|---|
| Docker Desktop | running, ≥ 8 GB memory allocated |
| [Astro CLI](https://docs.astronomer.io/astro/cli/install-cli) | `brew install astro` — it drives Airflow and loads `docker-compose.override.yml` automatically |
| Free ports | 8000, 3000, 8501, 8080, 5001, 9000, 9001 — or override them, see below |

---

## 2. Start the stack

```bash
git clone <repo-url> && cd Sales_Forecasting
cp .env.example .env

# REQUIRED: the API refuses to start without a signing key.
echo "JWT_SECRET_KEY=$(openssl rand -base64 48)" >> .env

astro dev start
```

`astro dev start` builds the API image and brings up every service in
`docker-compose.override.yml` alongside Airflow.

### When 3000 or 8000 are already taken

The documented ports are overridable because another local project commonly
owns them. **All four variables move together** — `NEXT_PUBLIC_API_URL` is the
URL the *browser* calls, so it must match `API_PORT`, and the API rejects any
origin not in `API_CORS_ALLOW_ORIGINS`, so that must match `FRONTEND_PORT`.
Changing only one produces a page that loads but cannot fetch:

```bash
API_PORT=8010 \
FRONTEND_PORT=3100 \
NEXT_PUBLIC_API_URL=http://localhost:8010 \
API_CORS_ALLOW_ORIGINS=http://localhost:3100,http://127.0.0.1:3100 \
astro dev start
```

Check what holds a port with `lsof -nP -iTCP:8000 -sTCP:LISTEN`.

> On the current development machine, 3000 and 8000 are held by an unrelated
> project, so **this stack runs on API `8010` / frontend `3100`** with exactly
> the overrides above. Every URL in the docs assumes the documented defaults;
> substitute accordingly.

Airflow's UI port is assigned by the Astro CLI and can drift off 8080. Read the
real one with:

```bash
docker port "$(docker ps --format '{{.Names}}' | grep api-server)" 8080
```

### Stop

```bash
astro dev stop     # keeps volumes
astro dev kill     # removes containers AND volumes — destroys the database
```

---

## 3. Services and ports

| Service | Container port | Host port | Purpose |
|---|---|---|---|
| `frontend` | 3000 | `${FRONTEND_PORT:-3000}` | Next.js web app — the user-facing surface |
| `api` | 8000 | `${API_PORT:-8000}` | FastAPI: auth, RBAC, inference, monitoring, registry |
| `streamlit-ui` | 8501 | 8501 | ML Operations Console (an API client only) |
| `api-server` (Airflow) | 8080 | 8080¹ | Airflow UI — training and monitoring DAGs |
| `mlflow` | 5001 | 5001 | tracking server + model registry |
| `minio` | 9000 | 9000 | S3-compatible artifact store (API) |
| `minio` console | 9001 | 9001 | MinIO web console |
| `scenario-db` | 5432 | — | PostgreSQL: the application's 13 tables |
| `mlflow-db` | 5432 | — | PostgreSQL: MLflow's backend store |
| `redis` | 6379 | — | Airflow broker |
| `minio-mc` | — | — | one-shot bucket provisioner |
| `scheduler`, `dag-processor`, `triggerer` | — | — | Airflow components |

¹ assigned by the Astro CLI; see above.

The two databases are deliberately separate: scenario, user, audit, and
monitoring tables are application state with their own lifecycle, and sharing
MLflow's store would couple the two.

---

## 4. Environment variables

Everything below has a working default in `docker-compose.override.yml`, with
**one exception**: `JWT_SECRET_KEY` is required and has no fallback.

### Required

| Variable | Notes |
|---|---|
| `JWT_SECRET_KEY` | **No generated fallback by design.** A generated fallback would silently invalidate every session on each restart and make two replicas reject each other's tokens. Generate with `openssl rand -base64 48`. Rotating it logs everyone out. |

### Ports and URLs

| Variable | Default | Notes |
|---|---|---|
| `API_PORT` | `8000` | host port for FastAPI |
| `FRONTEND_PORT` | `3000` | host port for Next.js |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | the URL the **browser** uses — never a Docker service name |
| `API_CORS_ALLOW_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | must include the frontend's origin |
| `MLFLOW_PUBLIC_URL` | `http://localhost:5001` | browsable MLflow URL for registry run links; distinct from the internal `MLFLOW_TRACKING_URI`, which a browser cannot resolve |

### Authentication

| Variable | Default | Notes |
|---|---|---|
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `45` | access token lifetime |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | refresh cookie lifetime |
| `JWT_ISSUER` | `sales-forecasting-api` | verified on decode |
| `JWT_AUDIENCE` | `sales-forecasting` | verified on decode |
| `COOKIE_SECURE` | `false` | **set `true` behind HTTPS.** `false` only for plain-HTTP local dev — a `Secure` cookie is dropped over http and sign-in fails silently |
| `COOKIE_SAMESITE` | `lax` | |
| `ALLOW_SELF_REGISTRATION` | `false` | closed by default: an open sign-up form would let anyone reach the model |
| `SELF_REGISTRATION_ROLE` | `VIEWER` | role granted to self-registered accounts |
| `MAX_LOGIN_ATTEMPTS` | `8` | then lockout |
| `LOGIN_LOCKOUT_MINUTES` | `15` | |
| `BCRYPT_ROUNDS` | `12` | |

### First-run bootstrap

| Variable | Default | Notes |
|---|---|---|
| `BOOTSTRAP_ADMIN_EMAIL` | `admin@example.com` | created only when the `users` table is empty |
| `BOOTSTRAP_ADMIN_NAME` | `Platform Administrator` | |
| `BOOTSTRAP_ADMIN_PASSWORD` | *(unset)* | unset ⇒ a strong one is generated and logged **once** |
| `MONITORING_SERVICE_EMAIL` | `monitoring-service@example.com` | the monitoring DAG's service account (`ML_ENGINEER`) |
| `MONITORING_SERVICE_PASSWORD` | *(unset)* | unset ⇒ no account is created and the DAG fails with a clear message rather than silently doing nothing. The API and the Airflow scheduler must see the **same** value |
| `MONITORING_SERVICE_NAME` | `Monitoring Service` | |

### Model resolution and forecast bounds

| Variable | Default | Notes |
|---|---|---|
| `MLFLOW_REGISTERED_MODEL_PREFIX` | `sales_forecast_models_` | registered names are `<prefix><model>` |
| `FORECAST_MODELS` | `ensemble,xgboost,lightgbm` | also the enumeration list, since `search_registered_models()` is unusable against the pinned MLflow server |
| `FORECAST_DEFAULT_MODEL` | `ensemble` | |
| `FORECAST_REQUIRE_PRODUCTION_STAGE` | `true` | `false` serves an unpromoted version — and the forecast says so in its warnings |
| `FORECAST_MIN_HISTORY_DAYS` | `30` | the models use windows up to 30 days; below that the deepest features come from a partial window |
| `FORECAST_MAX_HORIZON_DAYS` | `90` | |
| `FORECAST_PROFIT_TO_SALES_RATIO` | `0.3` | matches the M5 ingestion; a request supplying `profit` overrides it |

### Monitoring

| Variable | Default | Notes |
|---|---|---|
| `MONITORING_WINDOW_DAYS` | `28` | rolling scoring window |
| `MONITORING_SCHEDULE` | `@daily` | the DAG's schedule |
| `MONITORING_ACTUALS_LOOKBACK_DAYS` | `14` | ingestion lookback |
| `MONITORING_ACTUALS_PATH` | `/usr/local/airflow/data/raw/kaggle_m5/v1.0.0/sales` | where observed sales are read from |
| `MONITORING_API_URL` | `http://api:8000` | the internal URL the DAG calls |

### Scenario planner

| Variable | Default |
|---|---|
| `SCENARIO_ENABLED_TYPES` | `promotion,traffic,closure,holiday,price,custom` |
| `BASELINE_REQUIRE_PRODUCTION_STAGE` | `true` |
| `BASELINE_RESIDUAL_HORIZON_SCALING` | `true` |
| `BASELINE_FORECAST_TTL_HOURS` | `24` |
| `FEATURE_SCHEMA_ARTIFACT_PATH` | `feature_schema.json` |

### Infrastructure

| Variable | Default | Notes |
|---|---|---|
| `SCENARIO_DB_USER` / `_PASSWORD` / `_NAME` | `scenario` | **change outside local dev** |
| `SCENARIO_DATABASE_URL` | built from the above | also read as `DATABASE_URL` |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `minioadmin` | **change outside local dev.** Reaches the API and MLflow only; the browser never receives them |
| `MODEL_CACHE_DIR` | `/app/.cache/models` | backed by the `api-model-cache` volume |
| `LOG_LEVEL` | `INFO` | |
| `ALEMBIC_RENDER_AS_BATCH` | `true` | SQLite needs batch mode; set `false` to generate PostgreSQL-shaped migrations |
| `KAGGLE_USERNAME` / `KAGGLE_API_TOKEN` | — | only to re-ingest M5 from Kaggle; training reuses the parquet cache in `data/raw` |

### What the browser gets

Exactly one variable: `NEXT_PUBLIC_API_URL`. Verify it:

```bash
docker exec <project>-frontend-1 env | grep -Ei "JWT|AWS|MINIO|PASSWORD|SECRET|DATABASE|MLFLOW"
# expected: no output
```

The same holds for the Streamlit console, which receives only
`FORECAST_API_URL` — and whose image installs no ML or object-store library at
all, so it cannot load an artifact even by mistake.

---

## 5. Initial administrator

The first ADMIN is created automatically by the API's lifespan handler, **only
when the `users` table is empty**. There is no public "create the first admin"
endpoint to leave exposed.

### Option A — set the password yourself (recommended)

```bash
cat >> .env <<'EOF'
BOOTSTRAP_ADMIN_EMAIL=you@yourcompany.com
BOOTSTRAP_ADMIN_NAME=Your Name
BOOTSTRAP_ADMIN_PASSWORD=<a strong password>
EOF
astro dev restart
```

### Option B — let one be generated

Leave `BOOTSTRAP_ADMIN_PASSWORD` unset. The API generates a strong password and
writes it to its log **once**:

```bash
docker logs "$(docker ps --format '{{.Names}}' | grep -- '-api-1')" 2>&1 | grep -i bootstrap
```

Then sign in and change it from `/settings` immediately. The generated password
is not stored in plaintext anywhere and is not recoverable after the log
rotates — if you miss it, reset by clearing the `users` table (dev only) or by
using an existing admin.

### Then

1. Sign in at the frontend.
2. Change your password (`/settings`) — this also revokes all sessions.
3. Create the real accounts at `/admin/users`, giving each the least role that
   works: `VIEWER` → `BUSINESS_USER` → `ANALYST` → `ML_ENGINEER` → `ADMIN`.
4. Leave `ALLOW_SELF_REGISTRATION=false` unless you intend open sign-up.

The platform refuses to let you remove its own last administrator: you cannot
change your own role, deactivate yourself, or deactivate the last active ADMIN.

---

## 6. Database migrations

Schema is managed by Alembic. `api/migrations/env.py` reads the URL from the
environment (`SCENARIO_DATABASE_URL`, then `DATABASE_URL`) rather than from
`alembic.ini`, so **no credential is committed**.

```bash
# inside the API container (or with the env var exported locally)
docker exec -e PYTHONPATH=/app <api-container> alembic current
docker exec -e PYTHONPATH=/app <api-container> alembic upgrade head
docker exec -e PYTHONPATH=/app <api-container> alembic downgrade -1
```

The initial migration is **idempotent**: `_create_table`, `_create_index`,
`_drop_index`, and `_drop_table` wrappers inspect the live schema first, so it
applies cleanly onto a database that an earlier `create_all` already populated
— which is the state every pre-Alembic deployment is in.

Verified paths: fresh apply (14 tables including `alembic_version`, 75
indexes), re-run as a no-op, apply onto a `create_all` database, and
downgrade → re-upgrade.

To generate a new revision against PostgreSQL shapes rather than SQLite batch
mode:

```bash
ALEMBIC_RENDER_AS_BATCH=false alembic revision --autogenerate -m "describe it"
```

---

## 7. Train and promote a model

Nothing in the user-facing workflow requires this, but a fresh deployment needs
at least one Production version before forecasts can be generated.

1. Open the Airflow UI.
2. Enable and trigger **`sales_forecast_training`** (`@weekly`). It ingests the
   dataset, builds features, trains XGBoost / LightGBM / the ensemble, logs
   everything to MLflow, and registers the versions.
3. Promote a version to Production — from `/models` in the web app
   (`promote_model`) or in the MLflow UI.
4. Enable **`sales_forecast_monitoring`** (`@daily`) so accuracy and drift
   start accumulating. It needs `MONITORING_SERVICE_PASSWORD` set on **both**
   the API and the Airflow scheduler.

Until a version is in Production, the Forecast Console says so and disables the
Generate button. It does not fall back to a fabricated number.

---

## 8. Security configuration

### Before any shared or public deployment

- [ ] `JWT_SECRET_KEY` — unique, ≥ 32 bytes of entropy, never committed.
- [ ] `COOKIE_SECURE=true` and serve over HTTPS. The refresh cookie is
      `HttpOnly`, `SameSite=Lax`, and scoped to `Path=/auth`; without HTTPS it
      travels in the clear.
- [ ] `SCENARIO_DB_PASSWORD` — changed from `scenario`.
- [ ] `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` — changed from `minioadmin`.
- [ ] `BOOTSTRAP_ADMIN_PASSWORD` — set explicitly, or rotate immediately after
      the generated one is used.
- [ ] `ALLOW_SELF_REGISTRATION` — left `false`, or set with
      `SELF_REGISTRATION_ROLE=VIEWER`.
- [ ] `API_CORS_ALLOW_ORIGINS` — the real frontend origin only, never `*`.
- [ ] MLflow (5001), MinIO (9000/9001), and Airflow (8080) — **not published to
      the internet.** They have no authentication in this compose setup; they
      are reachable only from inside the Docker network and from the host.
- [ ] `.env` — not committed. `data/`, `mlflow.db`, and model artifacts are
      already git-ignored.
- [ ] The frontend image — confirm it carries no credential (the `docker exec
      env` check in § 4).

### What is already enforced in code

| | |
|---|---|
| Passwords | bcrypt at `BCRYPT_ROUNDS`; strength validated on set; **never stored or logged in plaintext**, and a generated password is displayed exactly once |
| Refresh tokens | stored only as **SHA-256 digests** — the database never holds a usable token |
| Rotation | single-use refresh; replaying a revoked token **revokes the whole family** |
| Lockout | `MAX_LOGIN_ATTEMPTS` then `LOGIN_LOCKOUT_MINUTES`, checked **before** the password so a locked account is not a password oracle |
| Enumeration | a missing account is verified against a real cached bcrypt hash, so timing matches a wrong password |
| Access token | returned in the body and held in **memory only** by the frontend — never `localStorage` |
| Authorization | capability checks in the **backend**; the frontend mirrors the server's own permission list for presentation only |
| Revocation | every request re-loads the user from the database, so deactivation and role changes take effect immediately, and an unrecognised role **fails closed** |
| Audit | append-only, including **denials**, which commit in their own transaction so a rolled-back request still leaves the record |
| Secret handling | `/health` reports dependency **status**, never URIs or credentials; `/v1/models/{v}/artifacts` returns metadata, never bytes or object-store paths |
| Error bodies | a `422` reports the field location and message, never echoing the caller's input |

### Not implemented

No SSO/OIDC, no MFA, no per-user API keys, and no rate limiting beyond login
lockout (`RATE_LIMITED` exists as a code but no global limiter is installed).
Put the API behind a gateway that provides those if you need them.

---

## 9. Persistence and backup

| Volume | Holds | Lose it and… |
|---|---|---|
| `scenario-db-volume` | **users, refresh tokens, audit log, monitoring, scenarios** — all 13 tables | every account and the entire audit trail are gone |
| `mlflow-db-volume` | MLflow runs, params, metrics, registry | model history and stage assignments are gone |
| `minio-data` | model artifacts | registered versions point at nothing; models must be retrained |
| `api-model-cache` | downloaded artifacts | harmless — re-fetched on the next forecast |
| `frontend-node-modules` | npm install | harmless — reinstalled |

Bind mounts: `./data` (datasets and manifests — read-only in the API),
`./frontend`, and `./ui`.

### Back up

```bash
P=$(docker ps --format '{{.Names}}' | grep -- '-scenario-db-1')
docker exec "$P" pg_dump -U scenario scenario | gzip > backup-app-$(date +%F).sql.gz

M=$(docker ps --format '{{.Names}}' | grep -- '-mlflow-db-1')
docker exec "$M" pg_dump -U mlflow mlflow | gzip > backup-mlflow-$(date +%F).sql.gz

# artifacts
docker run --rm -v minio-data:/d -v "$PWD":/b alpine \
  tar czf /b/backup-artifacts-$(date +%F).tar.gz -C /d .
```

### Restore

```bash
gunzip -c backup-app-YYYY-MM-DD.sql.gz | docker exec -i "$P" psql -U scenario scenario
```

Back up the application database and MLflow's **together** — a restore that
mixes a new audit log with an old registry leaves the two disagreeing about
which version is in Production.

**`astro dev kill` removes volumes.** Use `astro dev stop` unless you intend to
destroy the data.

---

## 10. Verifying a deployment

```bash
# 1. API is up and reports its dependencies
curl -s localhost:8000/health | python3 -m json.tool

# 2. A model is serving
curl -s localhost:8000/v1/models -H "Authorization: Bearer $T" | python3 -m json.tool

# 3. Auth is enforced -- expect 401 NOT_AUTHENTICATED
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/models

# 4. RBAC is enforced -- expect 403 PERMISSION_DENIED and an audit row
curl -s localhost:8000/v1/admin/users -H "Authorization: Bearer $ANALYST_TOKEN"

# 5. Every page serves
for p in / login register forecast scenario-planner monitoring models \
         admin/users admin/audit-logs settings; do
  printf '%s %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "localhost:3000/$p")" "$p"
done

# 6. No credential reached the browser bundle
docker exec <project>-frontend-1 env | grep -Ei "JWT|AWS|MINIO|PASSWORD|SECRET|DATABASE|MLFLOW"
```

Tests:

```bash
pytest api/tests tests/api tests/test_dataset_lineage.py -q   # 710 pass
cd frontend && npx tsc --noEmit && npm test && npm run build   # 278 pass, 11 routes

# live suite -- needs the stack up and an admin password
LIVE_API_URL=http://localhost:8000 LIVE_ADMIN_PASSWORD=… npm test -- live-api
```

---

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Bind for :::8000 failed: port is already allocated` | another project owns the port. Override all four variables together (§ 2). |
| API exits with `set JWT_SECRET_KEY` | required, no fallback. `echo "JWT_SECRET_KEY=$(openssl rand -base64 48)" >> .env`. |
| Page loads but every request fails CORS | `API_CORS_ALLOW_ORIGINS` does not list the frontend's origin, or `NEXT_PUBLIC_API_URL` does not match `API_PORT`. All four move together. |
| Sign-in appears to succeed, then immediately logs out | `COOKIE_SECURE=true` over plain HTTP — the browser drops the refresh cookie. Set `false` for local dev. |
| `astro dev start`: `No such image: …-api:latest` | the image tag or `pull_policy` was changed. Compose needs the explicit `image: ${API_IMAGE:-sales-forecasting-api:latest}` **and** `pull_policy: build`; without both, the CLI's `up` does not build the service. |
| Forecast returns `503 MODEL_NOT_AVAILABLE` | no version in Production. Promote one (§ 7), or set `FORECAST_REQUIRE_PRODUCTION_STAGE=false` to serve an unpromoted version — the response then warns about it. |
| `/v1/models` returns an empty list | an MLflow client/server version split: `search_registered_models()` returns `[]` from a 3.x client against a 2.9.2 server. The service enumerates `FORECAST_MODELS` instead, so check those names and `MLFLOW_REGISTERED_MODEL_PREFIX` match what training registered. |
| Monitoring shows the no-data message | expected until forecasts have been scored against actuals. The M5 actuals end **2016-04-24**, so a rolling window around today has no scorable pairs; run an explicit backtest window to see real numbers. |
| Monitoring DAG fails with a credentials message | `MONITORING_SERVICE_PASSWORD` is unset, or differs between the API and the Airflow scheduler. Both must see the same value. |
| A DAG import fails with `cannot import name 'DeclarativeBase'` | something in `dags/` imported `api.*`. Airflow 3 pins SQLAlchemy 1.4 and the API needs 2.0 — DAGs must reach the API over HTTP. |
| `streamlit-ui` reports unhealthy while serving fine | the old healthcheck ran `curl`, which `python:3.11-slim` does not have. The probe now uses the interpreter. |
| Frontend 404s a route that exists | stale `next dev` route cache. `docker restart <project>-frontend-1`. |
| Slow first forecast after a redeploy | the artifact cache is cold. It is persisted in `api-model-cache`, so this happens once. |

Logs:

```bash
docker logs -f "$(docker ps --format '{{.Names}}' | grep -- '-api-1')"
docker logs -f "$(docker ps --format '{{.Names}}' | grep -- '-frontend-1')"
astro dev logs --scheduler
```
