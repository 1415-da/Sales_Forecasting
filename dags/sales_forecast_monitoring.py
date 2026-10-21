"""
Monitoring DAG: score forecasts against actuals on a schedule.

This DAG is an **orchestrator**, not a second home for business logic. It
authenticates against the API and triggers
``POST /v1/monitoring/evaluate``, which loads recent actuals, scores forecasts
against them, measures drift, raises alerts, and returns what happened.

Why the work is not done here
-----------------------------
Airflow 3 pins SQLAlchemy 1.4, while the API's models use the 2.0 declarative
API. A task that imported ``api.db`` would fail at import with
``cannot import name 'DeclarativeBase'``. The alternative to calling the API
would be maintaining a second, 1.4-compatible copy of the schema, the
repositories, and the metric definitions -- two implementations that would
inevitably disagree about what WAPE means. Driving the API keeps one.

It also means the DAG's writes are attributable: it signs in as a normal
service account, so every evaluation appears in the audit log alongside human
actions rather than as an anonymous background mutation.

The rule the evaluation is built around: **it never invents data.** With no
scorable forecast it writes nothing and says so. A monitoring table seeded with
plausible zeros is worse than an empty one, because the zeros get believed.
"""

from datetime import datetime, timedelta

from airflow.decorators import dag, task

import json
import os
import urllib.error
import urllib.request

default_args = {
    "owner": "Sales Forecasting Team",
    "depends_on_past": False,
    "start_date": datetime(2025, 7, 22),
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}

#: Daily by default. Weekly suits a deployment where actuals land weekly;
#: scoring more often than outcomes arrive only produces empty windows.
MONITORING_SCHEDULE = os.getenv("MONITORING_SCHEDULE", "@daily")

#: Internal service name, not the host-published port: this call never leaves
#: the Docker network.
API_BASE_URL = os.getenv("MONITORING_API_URL", "http://api:8000")

#: Generous: the evaluation reads parquet, runs SQL aggregations, and may load
#: a model. A short timeout would fail a run that was about to succeed.
REQUEST_TIMEOUT_SECONDS = int(os.getenv("MONITORING_API_TIMEOUT", "600"))

ACTUALS_LOOKBACK_DAYS = int(os.getenv("MONITORING_ACTUALS_LOOKBACK_DAYS", "14"))
EVALUATION_WINDOW_DAYS = int(os.getenv("MONITORING_WINDOW_DAYS", "28"))
ALERT_STALE_DAYS = int(os.getenv("MONITORING_ALERT_STALE_DAYS", "30"))


def _request(method: str, path: str, body=None, token=None, timeout=None):
    """
    Call the API, returning ``(status_code, decoded_body)``.

    ``urllib`` rather than ``requests`` so the DAG adds no dependency to the
    Airflow image. Never raises for an HTTP error: the caller decides what a
    given status means, and a 503 from a starting API is worth a clear message
    rather than a stack trace.
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        f"{API_BASE_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout or REQUEST_TIMEOUT_SECONDS
        ) as response:
            return response.getcode(), json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except Exception:
            return error.code, {}
    except Exception as error:  # noqa: BLE001 - reported, not raised
        return 0, {"error": {"code": type(error).__name__, "message": str(error)}}


@dag(
    dag_id="sales_forecast_monitoring",
    schedule=MONITORING_SCHEDULE,
    start_date=datetime(2025, 7, 22),
    catchup=False,
    default_args=default_args,
    description="Score forecasts against actuals, measure drift, and raise alerts",
    tags=["ml", "monitoring", "sales"],
)
def sales_forecast_monitoring():
    @task()
    def check_api_task():
        """
        Confirm the API is reachable and report its dependency state.

        Separate from the evaluation so a failure here is unambiguous: "the API
        is down" and "the evaluation found nothing to score" are different
        problems and should not share a task.
        """
        code, body = _request("GET", "/health", timeout=30)
        if code != 200:
            raise RuntimeError(
                f"The API health endpoint returned {code}. Monitoring cannot "
                f"run until {API_BASE_URL} is reachable."
            )

        components = body.get("components", {})
        print(f"API status: {body.get('status')}")
        for name, component in components.items():
            print(f"  {name:<16} {component.get('status')}")

        model = body.get("production_model")
        if model:
            print(
                f"Production model: {model['model']} v{model['version']} "
                f"({model['stage']})"
            )
        else:
            print(
                "No Production model. Metrics will be recorded against "
                "'unknown' until one is promoted."
            )

        if components.get("database", {}).get("status") != "ok":
            raise RuntimeError(
                "The API cannot reach its database, so monitoring has nowhere "
                "to write. Check SCENARIO_DATABASE_URL on the api service."
            )
        return {"status": body.get("status"), "has_production_model": bool(model)}

    @task()
    def authenticate_task():
        """
        Sign in as the monitoring service account.

        Fails loudly when no credentials are configured rather than skipping
        silently: a monitoring DAG that quietly does nothing is worse than one
        that is visibly misconfigured, because the dashboard then goes stale
        without anyone being told.
        """
        email = os.getenv("MONITORING_SERVICE_EMAIL", "monitoring-service@example.com")
        password = os.getenv("MONITORING_SERVICE_PASSWORD", "")

        if not password:
            raise RuntimeError(
                "MONITORING_SERVICE_PASSWORD is not set. Set it on both the "
                "api service (which creates the account at start-up) and the "
                "Airflow services (which sign in with it)."
            )

        code, body = _request(
            "POST", "/auth/login", {"email": email, "password": password}, timeout=30
        )
        if code != 200 or "access_token" not in body:
            reason = body.get("error", {}).get("message", f"HTTP {code}")
            raise RuntimeError(
                f"Could not sign in as {email}: {reason}. The API creates this "
                "account at start-up when MONITORING_SERVICE_PASSWORD is set; "
                "confirm both services share the same value."
            )

        user = body.get("user", {})
        print(f"Signed in as {user.get('email')} ({user.get('role')})")
        # Only the token is returned. The password never enters an XCom.
        return {"token": body["access_token"], "role": user.get("role")}

    @task()
    def evaluate_task(health, credentials):
        """
        Run the evaluation and print what it produced.

        One call, because the steps share a transaction: metrics, drift, and
        alerts must all reflect the same window or the dashboard would show a
        verdict computed from inconsistent parts.
        """
        payload = {
            "actuals_lookback_days": ACTUALS_LOOKBACK_DAYS,
            "window_days": EVALUATION_WINDOW_DAYS,
            "alert_stale_days": ALERT_STALE_DAYS,
        }
        code, body = _request(
            "POST", "/v1/monitoring/evaluate", payload, token=credentials["token"]
        )

        if code != 200:
            error = body.get("error", {})
            raise RuntimeError(
                f"The evaluation failed with HTTP {code}: "
                f"{error.get('code')} {error.get('message')}"
            )

        actuals = body.get("actuals", {})
        print("=" * 68)
        print("MONITORING EVALUATION")
        print("=" * 68)
        print(f"  status            {body.get('status')}")
        print(f"  model version     {body.get('model_version')}")
        print(f"  window            {body.get('window_start')} .. {body.get('window_end')}")
        print()
        print("  Actuals")
        if actuals.get("skipped_reason"):
            print(f"    skipped: {actuals['skipped_reason']}")
        else:
            print(
                f"    {actuals.get('inserted', 0)} inserted, "
                f"{actuals.get('updated', 0)} corrected, from "
                f"{actuals.get('files_read', 0)} file(s)"
            )
            print(f"    range {actuals.get('date_min')} .. {actuals.get('date_max')}")
        print()
        print("  Accuracy")
        print(f"    scored store-days {body.get('scored_pairs', 0)}")
        print(f"    metrics written   {body.get('metrics_written', 0)}")
        for name, value in sorted((body.get("metrics") or {}).items()):
            print(f"    {name:<18} {value}")
        print()
        print("  Drift")
        if body.get("drift_skipped_reason"):
            print(f"    skipped: {body['drift_skipped_reason']}")
        else:
            print(
                f"    {body.get('drift_evaluated', 0)} series evaluated, "
                f"{body.get('drift_detected', 0)} drifting"
            )
        print()
        print("  Alerts")
        print(
            f"    {body.get('alerts_created', 0)} opened, "
            f"{body.get('alerts_refreshed', 0)} refreshed, "
            f"{body.get('alerts_auto_resolved', 0)} auto-resolved"
        )
        for title in body.get("alert_titles", []):
            print(f"    - {title}")
        print()
        print(f"  Recommendation    {body.get('recommendation', '').upper()}")
        print(f"  Reason            {body.get('recommendation_reason')}")
        if body.get("no_data_message"):
            print()
            print(f"  NOTE: {body['no_data_message']}")
        print("=" * 68)

        return {
            "status": body.get("status"),
            "scored_pairs": body.get("scored_pairs", 0),
            "metrics_written": body.get("metrics_written", 0),
            "alerts_created": body.get("alerts_created", 0),
            "recommendation": body.get("recommendation"),
        }

    @task()
    def report_task(evaluation):
        """
        Restate the outcome as the DAG's final word.

        Deliberately the last task: this is what an operator reads in the
        Airflow log to learn whether the platform is healthy without opening
        the dashboard.
        """
        status = evaluation.get("status")
        scored = evaluation.get("scored_pairs", 0)
        recommendation = evaluation.get("recommendation")

        print(f"Monitoring run complete: status={status}, scored={scored} store-day(s)")
        print(f"Recommendation: {recommendation}")

        if scored == 0:
            print(
                "Nothing was scorable. This is expected until forecasts have "
                "been generated and their outcome dates have passed; the "
                "dashboard reports 'no data' rather than a fabricated zero."
            )
        if recommendation == "retrain":
            print(
                "ACTION: retraining is recommended. Trigger "
                "sales_forecast_training, then compare the candidate against "
                "the current Production version before promoting it."
            )
        return evaluation

    health = check_api_task()
    credentials = authenticate_task()
    evaluation = evaluate_task(health, credentials)
    report_task(evaluation)


sales_forecast_monitoring()
