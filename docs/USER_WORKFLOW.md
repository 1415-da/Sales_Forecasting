# User workflows

What each kind of user can do, in the order they would do it. Every workflow
here is reachable from the browser alone — no Airflow, MLflow, MinIO, notebook,
or code access is required for any of them.

- [Signing in](#signing-in)
- [What each role sees](#what-each-role-sees)
- [Business user — generate a forecast](#business-user--generate-a-forecast)
- [Analyst — plan a scenario and read the detail](#analyst--plan-a-scenario-and-read-the-detail)
- [ML engineer — monitor, promote, roll back](#ml-engineer--monitor-promote-roll-back)
- [Administrator — users, roles, audit](#administrator--users-roles-audit)
- [Viewer — read-only](#viewer--read-only)
- [Everyone — settings](#everyone--settings)
- [Reading a forecast honestly](#reading-a-forecast-honestly)

---

## Signing in

1. Open the app (`http://localhost:3000` by default — `3100` on this machine,
   see [DEPLOYMENT.md](DEPLOYMENT.md)).
2. Any route redirects to **`/login`** until you are signed in.
3. Enter your email and password.

If self-registration is disabled — the default — the login page says
*"Accounts are created by an administrator"* instead of showing a dead sign-up
link. When `ALLOW_SELF_REGISTRATION=true`, **`/register`** is live and new
accounts receive `SELF_REGISTRATION_ROLE` (default `VIEWER`).

Your session refreshes itself in the background. After eight failed attempts
(`MAX_LOGIN_ATTEMPTS`) the account locks for 15 minutes
(`LOGIN_LOCKOUT_MINUTES`) and the page says so — an administrator can reset the
password to clear it immediately.

Signing out revokes the refresh token server-side, so the session cannot be
resumed from a stale cookie.

---

## What each role sees

The sidebar is built from the permission list the server returned for *your*
account, so you only see what you can use:

| Page | ADMIN | ML_ENGINEER | ANALYST | BUSINESS_USER | VIEWER |
|---|:--:|:--:|:--:|:--:|:--:|
| `/forecast` Forecast Console | ✅ | ✅ | ✅ | ✅ | ✅ read-only |
| `/scenario-planner` Scenario Planner | ✅ | ✅ | ✅ | ✅ | ✅ read-only |
| `/monitoring` Monitoring | ✅ | ✅ | ✅ +detail | ✅ summary | ✅ summary |
| `/models` Model Registry | ✅ | ✅ +actions | ✅ +detail | ✅ list | ✅ list |
| `/admin/users` Users | ✅ | — | — | — | — |
| `/admin/audit-logs` Audit Log | ✅ | ✅ | — | — | — |
| `/settings` Settings | ✅ | ✅ | ✅ | ✅ | ✅ |

Hiding a button is presentation only. The API enforces the same matrix, so
calling a forbidden endpoint directly returns `403` **and records a denial in
the audit log**.

---

## Business user — generate a forecast

**Role:** `BUSINESS_USER` · **Needs:** `generate_forecast`, `export_forecast`

1. Open **Forecast Console** (`/forecast`). The header shows which model
   version is currently serving Production, so you know what produced the
   number.
2. **Pick a store** and a **horizon** (1–90 days).
3. **Choose a model** — `ensemble` (default), `xgboost`, or `lightgbm` — and a
   **confidence level**, 80% or 95%.
4. **Supply history.** Either:
   - click **Load sample history** for a ready 60-day series, or
   - paste/enter your own daily sales. At least 30 days
     (`FORECAST_MIN_HISTORY_DAYS`) are required, in date order, with no
     duplicate dates. You are told exactly which rule failed if one does.

   You are never asked for `is_holiday`, `customer_traffic`, `quantity_sold`,
   or `profit` — the API derives all of them the way training did.
5. **Generate Forecast.**
6. Read the result:
   - the **forecast line** with its **uncertainty band**,
   - **KPIs** — total, daily average, peak day, and the band width,
   - the **daily table**, and
   - **Download CSV** for the same numbers.
7. Check the **warnings** panel. It tells you what was assumed — which features
   were imputed, how the interval was derived, and whether the serving model is
   actually in the Production stage.

**If no model is in Production**, the console says so and the Generate button is
disabled. It does not invent a forecast.

---

## Analyst — plan a scenario and read the detail

**Role:** `ANALYST` · **Needs:** everything above plus `run_scenario`,
`view_model_registry_detail`, `view_monitoring_detail`

### Run a what-if

1. Open **Scenario Planner** (`/scenario-planner`).
2. A **baseline** is established first — the same recursive forecast as the
   console, checkpointed so the comparison is against a fixed reference.
3. Pick a **scenario type**. The planner only offers what the selected model
   can express:
   - **Available** — `promotion`, `traffic`, `holiday`, `custom`
   - **Unavailable** — `closure` and `price` are greyed out with the reason:
     the current model has no corresponding feature. Selecting one via the API
     returns `409`, not a guess.
4. Set the levers and the affected date range, then **Compare**.
5. Read **baseline vs scenario** — the two paths overlaid, per-day and total
   deltas, and the percentage change.
6. **Read the inertness warning if it appears.** For this model,
   `promotion`, `traffic`, and `holiday` produce a near-zero delta because
   those features were effectively constant in the training data, so the model
   learned almost no response to them. The warning says the flatness is a
   property of the model — it is **not** evidence that promotions do not move
   sales. Treating it as a business finding would be wrong.
7. `/scenario-planner` keeps the comparison; **Roll back** reverts it while
   preserving the audit trail.

### Read the detail views

- **Monitoring** → the detailed tab adds per-window metrics and the full
  per-feature drift table (PSI and KS with their verdicts), not just the
  headline.
- **Model Registry** → version detail, full metric sets, **lineage** (the
  training run, dataset version, parameters), and artifact metadata.
  **Compare** two versions side by side with direction-aware deltas.

An analyst cannot change any model. Promote, archive, and roll back are not
rendered, and the endpoints return `403`.

---

## ML engineer — monitor, promote, roll back

**Role:** `ML_ENGINEER` · **Needs:** everything above plus `promote_model`,
`archive_model`, `rollback_model`, `trigger_retraining`, `manage_alerts`,
`view_audit_logs`

### Daily check

1. **Monitoring** (`/monitoring`) — read in this order:
   - **Health summary** and the **retraining recommendation**
     (`healthy` / `investigate` / `retrain`).
   - **Data freshness** — how current the actuals and forecasts are. Stale
     actuals explain a missing accuracy window.
   - **Accuracy** — **WAPE is the headline** (MAPE explodes on this dataset's
     zero-sales days; it is still shown, masked to non-zero actuals), with MAE,
     RMSE, signed bias, **interval coverage**, and R², plus the trend.
   - **Drift** — PSI and KS per series. What is measured is **prediction**
     drift (`forecast_error`, `predicted_sales`, `actual_sales`), not
     input-feature drift. The page states this.
   - **Alerts** — **Acknowledge** one you are working on, **Resolve** one that
     is handled. Alerts are fingerprint-deduplicated, so a persistent breach
     does not re-alert every day.

   Before any forecast has been scored against an actual, the page shows the
   explicit empty state: *"Monitoring will populate after forecasts and actual
   outcomes are recorded."* No placeholder chart is drawn.

2. Monitoring also runs itself: the Airflow DAG `sales_forecast_monitoring`
   calls `POST /v1/monitoring/evaluate` **@daily** as a service account. You
   can trigger the same evaluation from the UI.

### Promote a model

1. **Model Registry** (`/models`) → open the candidate version.
2. **Compare** it against the current Production version. Deltas are
   direction-aware: lower is better for error metrics, higher for R².
3. Check **lineage** — run, dataset version, parameters — so you know what
   produced it.
4. **Promote**. Confirm the dialog. The API then applies the transition,
   **re-reads the registry to verify it**, writes an audit row, and returns the
   fresh state. A transition that claimed success without being verified is
   what previously left the registry with no Production version at all.
5. **Roll back** restores the previous Production version by the same path.
   **Archive** retires a version.

Every one of these writes an `audit_logs` row with your identity, the version,
and the outcome.

---

## Administrator — users, roles, audit

**Role:** `ADMIN` · **Needs:** all 18 permissions, including `manage_users`
and `view_all_audit_logs`

### First sign-in

The first ADMIN is created automatically when the `users` table is empty, from
`BOOTSTRAP_ADMIN_EMAIL`. If `BOOTSTRAP_ADMIN_PASSWORD` was left unset, a strong
password was generated and written to the API log **once** — see
[DEPLOYMENT.md § Initial administrator](DEPLOYMENT.md#initial-administrator).
Change it from `/settings` immediately.

### Manage users

**Users** (`/admin/users`):

- **Create a user** — email, name, role. Either set a password or let one be
  generated; a generated password is shown **once**, at creation, and is never
  stored or displayed in plaintext again.
- **Edit** — name and role.
- **Deactivate / Reactivate** — a deactivated user is refused at login
  (`403 ACCOUNT_INACTIVE`), and because every request re-loads the user from
  the database, an existing session stops working immediately rather than at
  token expiry.
- **Reset password** — issues a new one and ends the user's sessions.

Three guards exist so the platform cannot be locked out of:

- you cannot change **your own** role,
- you cannot deactivate **yourself**, and
- you cannot remove or deactivate the **last active ADMIN**.

### Read the audit trail

**Audit Log** (`/admin/audit-logs`) — append-only, filterable by actor, action,
resource, outcome, and date range. It includes `outcome=denied` rows, so an
attempt to use a permission someone does not hold is visible, not just the
successful actions.

---

## Viewer — read-only

**Role:** `VIEWER` · **Needs:** the five read capabilities

A viewer can open the Forecast Console, Scenario Planner, Monitoring, and the
Model Registry list, and read what is already there — existing forecasts,
summary health, model versions and their headline performance.

A viewer **cannot** generate a forecast, run a scenario, export, see the
detailed monitoring or registry views, manage alerts, change any model, or see
the audit log. The buttons are absent, and the endpoints return `403`.

---

## Everyone — settings

**Settings** (`/settings`) — available to every role:

- your profile: name, email, role, and **the exact permission list the server
  grants you**, rendered from `GET /v1/admin/permissions` and `GET /auth/me`,
  so what you read is what is enforced;
- **change your password** — requires the current one, enforces the strength
  rules (all failures are reported at once, not one per attempt), and
  **revokes every session**, so a password change logs you out everywhere;
- your **live sessions**.

---

## Reading a forecast honestly

The platform reports measured numbers, including the inconvenient ones. Three
things every user of these forecasts should know:

1. **The training metrics overstate accuracy badly.** Training reports
   WAPE ≈ 1.1% and R² ≈ 0.995. A real out-of-sample backtest measured
   **WAPE 18.2%, MAE 779, RMSE 956, bias +292, R² 0.458**. The cause is target
   leakage in the feature set — `quantity_sold` equals `sales`, and `profit` is
   exactly `0.3 × sales`. Trust the monitoring numbers, not the training card.

2. **The 95% interval is over-confident.** Measured coverage was **55%**,
   not 95%. Monitoring raised that alert on its own. Treat the band as
   indicative, not as a calibrated 95% interval.

3. **Scenario levers are near-inert for this model**, as described above. The
   platform tells you this rather than printing a fabricated uplift.

Fixing (1) and (2) requires a feature allowlist change and a retrain — see
[README § Known limitations](../README.md#known-limitations).
