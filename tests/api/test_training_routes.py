"""
Training workspace routes, end to end through FastAPI with fake services.

Object storage and Airflow are replaced by in-memory doubles that implement the
same surface, so the tests exercise the real routes, services, database rows,
permissions and audit trail -- and can make Airflow time out, refuse, or fail
on demand.
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.core.rbac import Role
from api.db.models.training import TrainingRequest
from api.db.session import session_scope
from api.deps import get_airflow_client, get_dataset_store, get_forecast_service, get_registry_admin_service
from api.main import create_app
from api.services.airflow_client import AirflowClient, AirflowConflict, AirflowTimeout, AirflowUnavailable
from api.services.registry_admin_service import MutationResult, RegistryAction
from training import storage


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class MemoryStore:
    """``DatasetStore`` semantics (write-once) without MinIO."""

    def __init__(self):
        self.objects: Dict[str, bytes] = {}
        self.shas: Dict[str, str] = {}

    def put_once(self, key, content, sha256, content_type):
        if key in self.objects:
            if self.shas[key] == sha256:
                return
            raise storage.ImmutableObjectError(key)
        self.objects[key] = content
        self.shas[key] = sha256

    def get(self, key):
        return self.objects[key]

    def download(self, key, path):
        with open(path, "wb") as handle:
            handle.write(self.objects[key])


class FakeAirflow:
    def __init__(self):
        self.paused = False
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.triggers: List[Dict[str, Any]] = []
        self.trigger_error: Optional[Exception] = None
        self.tasks: List[Dict[str, Any]] = []
        self.summary: Optional[Dict[str, Any]] = None
        self.unavailable = False

    def get_dag(self, dag_id):
        return {"dag_id": dag_id, "is_paused": self.paused}

    def trigger(self, dag_id, dag_run_id, conf, note=""):
        self.triggers.append({"dag_id": dag_id, "dag_run_id": dag_run_id, "conf": conf})
        if dag_run_id in self.runs:
            raise AirflowConflict("exists", 409)
        self.runs[dag_run_id] = {"dag_run_id": dag_run_id, "state": "queued", "start_date": None, "end_date": None}
        if self.trigger_error is not None:
            error, self.trigger_error = self.trigger_error, None
            raise error
        return self.runs[dag_run_id]

    def get_run(self, dag_id, dag_run_id):
        if self.unavailable:
            raise AirflowUnavailable("down")
        return self.runs.get(dag_run_id)

    def task_instances(self, dag_id, dag_run_id):
        return list(self.tasks)

    def xcom(self, dag_id, dag_run_id, task_id, key="return_value"):
        return self.summary

    def run_url(self, dag_id, dag_run_id):
        return f"http://airflow.test/dags/{dag_id}/runs/{dag_run_id}"


def csv_bytes(stores=("CA_1", "TX_1"), start="2015-01-01", days=200, base=1000.0) -> bytes:
    rows = [
        {"date": day.strftime("%Y-%m-%d"), "store_id": store, "sales": base + i * 100 + (day - pd.Timestamp("2015-01-01")).days}
        for i, store in enumerate(stores)
        for day in pd.date_range(start, periods=days)
    ]
    return pd.DataFrame(rows).to_csv(index=False).encode()


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def airflow():
    return FakeAirflow()


@pytest.fixture
def client(store, airflow):
    app = create_app()
    app.dependency_overrides[get_dataset_store] = lambda: store
    app.dependency_overrides[get_airflow_client] = lambda: airflow
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.app_ref = app
        yield test_client


def upload(client, content=None, name=None, filename="sales.csv", **form):
    data = {"name": name or f"ds-{uuid.uuid4().hex[:8]}", "mode": "independent", "currency": "USD", "source_type": "real"}
    data.update(form)
    return client.post(
        "/v1/training/datasets",
        files={"file": (filename, io.BytesIO(content if content is not None else csv_bytes()), "text/csv")},
        data=data,
    )


def ready_dataset(client, **kwargs) -> Dict[str, Any]:
    created = upload(client, **kwargs)
    assert created.status_code == 201, created.text
    sealed = client.post(f"/v1/training/datasets/{created.json()['id']}/validate")
    assert sealed.status_code == 200, sealed.text
    assert sealed.json()["status"] == "ready"
    return sealed.json()


def start(client, dataset_id, key=None, config="standard_v1"):
    return client.post(
        "/v1/training/runs",
        json={"dataset_id": dataset_id, "training_config_id": config},
        headers={"Idempotency-Key": key or f"key-{uuid.uuid4().hex}"},
    )


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.ANALYST, Role.ML_ENGINEER, Role.ADMIN])
def test_analysts_and_above_can_upload(client, as_role, role):
    as_role(role)
    response = upload(client)
    assert response.status_code == 201
    assert response.json()["status"] == "validated"


@pytest.mark.parametrize("role", [Role.VIEWER, Role.BUSINESS_USER])
def test_other_roles_cannot_upload_or_start_training(client, as_role, role):
    as_role(role)
    assert upload(client).status_code == 403
    assert start(client, uuid.uuid4().hex).status_code == 403


def test_an_oversized_file_is_refused_before_it_is_stored(client, store, monkeypatch):
    monkeypatch.setenv("TRAINING_UPLOAD_MAX_BYTES", "1000")
    response = upload(client)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
    assert store.objects == {}


def test_a_pickle_is_never_accepted(client):
    response = upload(client, content=b"\x80\x04\x95", filename="model.pkl")
    assert response.status_code == 422


def test_a_malformed_file_is_stored_as_invalid_with_an_actionable_report(client):
    bad = b"date,store_id,sales\n2015-01-01,CA_1,\nnot-a-date,CA_1,5\n"
    response = upload(client, content=bad)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "invalid"
    codes = {issue["code"] for issue in body["validation_report"]["errors"]}
    assert {"NULL_TARGET", "INVALID_DATE_VALUES"} <= codes
    sealed = client.post(f"/v1/training/datasets/{body['id']}/validate")
    assert sealed.status_code == 409  # an invalid session can never become a version


def test_store_scope_is_enforced(client, monkeypatch):
    monkeypatch.setenv("TRAINING_ALLOWED_STORES", "CA_1")
    body = upload(client).json()
    assert "STORE_OUT_OF_SCOPE" in {issue["code"] for issue in body["validation_report"]["errors"]}


def test_storage_keys_are_generated_not_taken_from_the_filename(client, store):
    upload(client, filename="../../etc/passwd.csv")
    assert store.objects
    for key in store.objects:
        assert key.startswith("datasets/") and "passwd" not in key and ".." not in key


# ---------------------------------------------------------------------------
# Sealing and immutability
# ---------------------------------------------------------------------------


def test_sealing_writes_hashed_immutable_objects_and_a_manifest(client, store):
    dataset = ready_dataset(client)
    prefix = f"datasets/{dataset['id']}/"
    keys = sorted(key for key in store.objects if key.startswith(prefix))
    assert any("/source/" in key for key in keys)
    assert any("/processed/" in key for key in keys)
    manifest = store.get(f"{prefix}manifest.json")
    import hashlib, json

    assert hashlib.sha256(manifest).hexdigest() == dataset["manifest_sha256"]
    body = json.loads(manifest)
    assert body["processed"]["content_sha256"] == dataset["content_sha256"]
    assert body["row_count"] == 400 and body["store_count"] == 2
    assert body["source_types"] == ["real"]
    assert body["validation"]["split"]["valid"]
    # Sealing again changes nothing.
    again = client.post(f"/v1/training/datasets/{dataset['id']}/validate").json()
    assert again["manifest_sha256"] == dataset["manifest_sha256"]


def test_identical_content_hashes_identically_across_versions(client):
    first = ready_dataset(client, name="same")
    second = ready_dataset(client, name="same")
    assert second["version"] == first["version"] + 1
    assert second["content_sha256"] == first["content_sha256"]
    assert second["id"] != first["id"]


def test_a_stored_object_is_never_overwritten():
    store = MemoryStore()
    store.put_once("datasets/x", b"a", "1", "text/plain")
    store.put_once("datasets/x", b"a", "1", "text/plain")
    with pytest.raises(storage.ImmutableObjectError):
        store.put_once("datasets/x", b"b", "2", "text/plain")


def test_the_real_store_refuses_to_overwrite_different_content():
    class S3:
        def __init__(self):
            self.objects = {}

        def head_bucket(self, Bucket):
            return {}

        def head_object(self, Bucket, Key):
            if Key not in self.objects:
                raise type("E", (Exception,), {"response": {"Error": {"Code": "404"}}})()
            return {"Metadata": {"sha256": self.objects[Key][1]}}

        def put_object(self, Bucket, Key, Body, ContentType, Metadata):
            self.objects[Key] = (Body, Metadata["sha256"])

    real = storage.DatasetStore(client=S3(), bucket="b")
    real.put_once("datasets/k", b"one", "1", "text/plain")
    real.put_once("datasets/k", b"one", "1", "text/plain")
    with pytest.raises(storage.ImmutableObjectError):
        real.put_once("datasets/k", b"two", "2", "text/plain")


def test_append_combines_and_leaves_the_base_version_unchanged(client, store):
    base = ready_dataset(client, name="history")
    before = dict(store.objects)
    newer = upload(
        client,
        content=csv_bytes(start="2015-07-10", days=30),
        name="history",
        mode="append",
        base_dataset_id=base["id"],
    ).json()
    assert newer["status"] == "validated", newer["validation_report"]["errors"]
    combination = newer["validation_report"]["combination"]
    assert combination["exact_duplicates_removed"] > 0
    assert newer["row_count"] == combination["rows"]
    for key, value in before.items():
        assert store.objects[key] == value


def test_appending_synthetic_data_to_real_history_is_rejected(client):
    base = ready_dataset(client)
    response = upload(
        client,
        content=csv_bytes(start="2015-07-10", days=30),
        mode="append",
        base_dataset_id=base["id"],
        source_type="synthetic",
    ).json()
    assert response["status"] == "invalid"
    assert "MIXED_SOURCE_TYPES" in {issue["code"] for issue in response["validation_report"]["errors"]}


def test_conflicting_appends_need_the_correction_policy(client):
    base = ready_dataset(client)
    conflicting = csv_bytes(start="2015-07-10", days=30, base=1.0)
    rejected = upload(client, content=conflicting, mode="append", base_dataset_id=base["id"]).json()
    assert "CONFLICTING_RECORDS" in {issue["code"] for issue in rejected["validation_report"]["errors"]}
    corrected = upload(
        client, content=conflicting, mode="append", base_dataset_id=base["id"], conflict_policy="prefer_new"
    ).json()
    assert corrected["status"] == "validated"
    assert corrected["validation_report"]["combination"]["corrections_applied"] > 0


# ---------------------------------------------------------------------------
# Training requests and Airflow
# ---------------------------------------------------------------------------


def test_the_trigger_carries_only_server_resolved_metadata(client, airflow):
    dataset = ready_dataset(client)
    response = start(client, dataset["id"])
    assert response.status_code == 201, response.text
    run = response.json()
    (call,) = airflow.triggers
    assert call["dag_run_id"] == run["dag_run_id"] == f"training__{uuid.UUID(run['id']).hex}"
    assert call["conf"] == {
        "training_request_id": uuid.UUID(run["id"]).hex,
        "dataset_id": dataset["id"],
        "dataset_version": dataset["version"],
        "manifest_uri": dataset["manifest_uri"],
        "manifest_sha256": dataset["manifest_sha256"],
        "content_sha256": dataset["content_sha256"],
        "training_config_id": "standard_v1",
        "deployment_policy": "manual_review",
    }
    assert run["status"] == "queued"


def test_a_double_submission_starts_one_run(client, airflow):
    dataset = ready_dataset(client)
    first = start(client, dataset["id"], key="same-key-123")
    second = start(client, dataset["id"], key="same-key-123")
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(airflow.triggers) == 1


def test_reusing_a_key_for_a_different_request_is_refused(client):
    first = ready_dataset(client)
    other = ready_dataset(client)
    start(client, first["id"], key="reused-key-1")
    response = start(client, other["id"], key="reused-key-1")
    assert response.status_code == 409


def test_unsealed_data_and_unapproved_configurations_are_refused(client, as_role):
    draft = upload(client).json()
    assert start(client, draft["id"]).status_code == 409
    dataset = ready_dataset(client)
    assert start(client, dataset["id"], config="anything_goes").status_code == 422
    as_role(Role.ANALYST)
    assert start(client, dataset["id"], config="tuned_v1").status_code == 403


def test_a_paused_dag_is_reported_and_nothing_is_recorded(client, airflow):
    dataset = ready_dataset(client)
    airflow.paused = True
    response = start(client, dataset["id"])
    assert response.status_code == 502
    assert "paused" in response.json()["error"]["message"]
    assert airflow.triggers == []


def test_a_timeout_is_reconciled_by_run_id_without_a_duplicate(client, airflow):
    dataset = ready_dataset(client)
    airflow.trigger_error = AirflowTimeout("slow")
    run = start(client, dataset["id"]).json()
    assert run["status"] == "submission_uncertain"
    # The run did land; the status read finds it by its known id.
    refreshed = client.get(f"/v1/training/runs/{run['id']}").json()
    assert refreshed["status"] == "queued"
    assert len(airflow.triggers) == 1


def test_a_lost_submission_is_resubmitted_under_the_same_run_id(client, airflow, monkeypatch):
    dataset = ready_dataset(client)
    airflow.trigger_error = AirflowUnavailable("refused")
    run = start(client, dataset["id"]).json()
    airflow.runs.clear()  # it never arrived
    assert run["status"] == "pending"
    refreshed = client.get(f"/v1/training/runs/{run['id']}").json()
    assert refreshed["status"] == "queued"
    assert [call["dag_run_id"] for call in airflow.triggers] == [run["dag_run_id"]] * 2


def _finish(airflow, run, state, summary=None, tasks=None):
    airflow.runs[run["dag_run_id"]].update(
        {"state": state, "start_date": "2026-01-01T00:00:00Z", "end_date": "2026-01-01T00:05:00Z"}
    )
    airflow.tasks = tasks or []
    airflow.summary = summary


def summary_for(run, version="7", gate_passed=True, stores=("CA_1", "TX_1")):
    return {
        "training_request_id": uuid.UUID(run["id"]).hex,
        "mlflow_run_id": f"run-{uuid.UUID(run['id']).hex}",
        "candidates": [
            {"model": "ensemble", "version": version, "registered_name": "sales_forecast_models_ensemble", "gate_passed": gate_passed}
        ],
        "evaluation": {
            "metrics": {"ensemble": {"wape": 7.9, "rmse": 470.0, "mae": 315.0}},
            "compatibility": {"target": "sales", "frequency": "D", "currency": "USD", "supported_stores": list(stores)},
        },
    }


def test_progress_is_real_task_state_and_survives_a_refresh(client, airflow):
    dataset = ready_dataset(client)
    run = start(client, dataset["id"]).json()
    airflow.runs[run["dag_run_id"]]["state"] = "running"
    airflow.tasks = [
        {"task_id": "train_models_task", "state": "running", "try_number": 1},
        {"task_id": "extract_data_task", "state": "success", "try_number": 1},
    ]
    detail = client.get(f"/v1/training/runs/{run['id']}").json()
    assert detail["status"] == "running"
    assert [task["task_id"] for task in detail["task_states"]] == ["extract_data_task", "train_models_task"]
    # Airflow goes away; a refresh still shows the persisted progress.
    airflow.unavailable = True
    again = client.get(f"/v1/training/runs/{run['id']}").json()
    assert again["task_states"] == detail["task_states"]
    assert again["sync_warning"]


def test_a_successful_run_registers_candidates_for_review(client, airflow):
    dataset = ready_dataset(client)
    run = start(client, dataset["id"]).json()
    _finish(airflow, run, "success", summary_for(run))
    detail = client.get(f"/v1/training/runs/{run['id']}").json()
    assert detail["status"] == "succeeded"
    assert detail["deployment_state"] == "awaiting_review"
    assert detail["candidates"][0]["version"] == "7"
    assert detail["is_terminal"]


def test_a_failed_run_explains_itself_and_changes_nothing(client, airflow):
    dataset = ready_dataset(client)
    run = start(client, dataset["id"]).json()
    _finish(
        airflow,
        run,
        "failed",
        tasks=[
            {"task_id": "train_models_task", "state": "failed", "try_number": 2},
            {"task_id": "register_best_model_task", "state": "upstream_failed"},
        ],
    )
    detail = client.get(f"/v1/training/runs/{run['id']}").json()
    assert detail["status"] == "failed"
    assert "train_models_task" in detail["failure_reason"]
    assert detail["candidates"] == [] and detail["deployment_state"] == "not_ready"


def test_the_airflow_link_is_for_ml_engineers_only(client, as_role):
    dataset = ready_dataset(client)
    run = start(client, dataset["id"]).json()
    as_role(Role.ANALYST)
    assert client.get(f"/v1/training/runs/{run['id']}").json()["airflow_url"] is None
    as_role(Role.ML_ENGINEER)
    assert client.get(f"/v1/training/runs/{run['id']}").json()["airflow_url"]


# ---------------------------------------------------------------------------
# Decisions and deployment
# ---------------------------------------------------------------------------


class Version(SimpleNamespace):
    @property
    def is_production(self):
        return self.stage == "Production"

    def lineage(self):
        return {}


class FakeForecasts:
    def __init__(self, served=("CA_1", "TX_1")):
        self.versions = [
            Version(model="ensemble", version="7", stage="None", run_id=None, metrics={}),
            Version(model="ensemble", version="2", stage="Production", run_id="run-prod", metrics={}),
        ]
        self.served = list(served)
        self.settings = SimpleNamespace(recommended_history_days=60, min_history_days=30, default_model="ensemble")
        self.artifacts = SimpleNamespace(
            try_load_bundle=lambda run_id, model: SimpleNamespace(supported_stores=self.served),
            clear_cache=lambda: None,
        )
        self.registry = SimpleNamespace(list_versions=lambda model: list(self.versions), known_models=lambda: ("ensemble",))
        self.projected = []

    def project(self, *, resolved, history, store_id, horizon_days):
        self.projected.append((resolved.version, store_id, len(history)))
        start = pd.Timestamp(history[-1].date) + pd.Timedelta(days=1)
        return SimpleNamespace(predictions=[(start + pd.Timedelta(days=i), 100.0) for i in range(horizon_days)])

    def list_models(self):
        return SimpleNamespace(models=[])


class FakeAdmin:
    def __init__(self, forecasts):
        self.settings = SimpleNamespace(default_model="ensemble")
        self.promoted = []
        self.forecasts = forecasts

    def promote(self, *, model, version, archive_existing=True):
        self.promoted.append((model, version))
        for item in self.forecasts.versions:
            item.stage = "Production" if item.version == version else "Archived"
        return MutationResult(
            action=RegistryAction.PROMOTE, model=model, version=version,
            previous_stage="None", new_stage="Production", archived_versions=["2"], verified=True,
        )


@pytest.fixture
def deployment(client, airflow):
    forecasts = FakeForecasts()
    admin = FakeAdmin(forecasts)
    client.app_ref.dependency_overrides[get_forecast_service] = lambda: forecasts
    client.app_ref.dependency_overrides[get_registry_admin_service] = lambda: admin
    dataset = ready_dataset(client)
    run = start(client, dataset["id"]).json()
    _finish(airflow, run, "success", summary_for(run))
    client.get(f"/v1/training/runs/{run['id']}")
    # The registry's v7 is this run's artifacts.
    forecasts.versions[0].run_id = f"run-{uuid.UUID(run['id']).hex}"
    return SimpleNamespace(run=run, forecasts=forecasts, admin=admin)


def promote(client, version="7", **body):
    payload = {"confirm": True, "model": "ensemble", "reason": "Better on recent data", "expected_identity": f"ensemble:{version}"}
    payload.update(body)
    return client.post(f"/v1/models/{version}/promote", json=payload)


def test_an_analyst_cannot_deploy_or_reject(client, deployment, as_role):
    as_role(Role.ANALYST)
    assert promote(client).status_code == 403
    response = client.post(
        f"/v1/training/runs/{deployment.run['id']}/reject",
        json={"model": "ensemble", "version": "7", "reason": "not convinced"},
    )
    assert response.status_code == 403
    assert deployment.admin.promoted == []


def test_approval_needs_the_exact_identity_and_a_reason(client, deployment):
    assert promote(client, expected_identity="ensemble:8").status_code == 422
    assert promote(client, reason="").status_code == 422
    assert deployment.admin.promoted == []


def test_approval_smoke_tests_then_deploys_and_records_the_decision(client, deployment, as_role):
    as_role(Role.ML_ENGINEER)
    response = promote(client)
    assert response.status_code == 200, response.text
    assert deployment.admin.promoted == [("ensemble", "7")]
    assert deployment.forecasts.projected and deployment.forecasts.projected[0][0] == "7"
    detail = client.get(f"/v1/training/runs/{deployment.run['id']}").json()
    assert detail["deployment_state"] == "deployed"
    assert (detail["deployed_model"], detail["deployed_version"]) == ("ensemble", "7")
    (decision,) = detail["decisions"]
    assert decision["decision"] == "approved"
    assert decision["previous_production_version"] == "2"
    assert decision["decided_by_role"] == "ML_ENGINEER"
    assert {item["check"] for item in decision["checks"]["checks"]} >= {"store_coverage", "smoke_forecast"}


def test_a_registry_version_from_another_run_is_not_treated_as_this_candidate(client, deployment):
    deployment.forecasts.versions[0].run_id = "some-other-run"
    response = promote(client)
    # Not a training candidate any more, so the candidate gates do not apply
    # and nothing is recorded against this request.
    assert response.status_code == 200
    detail = client.get(f"/v1/training/runs/{deployment.run['id']}").json()
    assert detail["decisions"] == [] and detail["deployment_state"] == "awaiting_review"


def test_a_candidate_for_a_different_store_domain_is_blocked(client, deployment):
    deployment.forecasts.served = ["CA_1", "TX_1", "WI_1"]
    response = promote(client)
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "DEPLOYMENT_BLOCKED" and "WI_1" in body["message"]
    assert deployment.admin.promoted == []


def test_a_failing_smoke_forecast_blocks_deployment(client, deployment):
    def broken(**kwargs):
        raise RuntimeError("cannot load")

    deployment.forecasts.project = broken
    response = promote(client)
    assert response.status_code == 409
    assert deployment.admin.promoted == []


def test_rejection_is_recorded_without_touching_the_registry(client, deployment, as_role):
    as_role(Role.ML_ENGINEER)
    response = client.post(
        f"/v1/training/runs/{deployment.run['id']}/reject",
        json={"model": "ensemble", "version": "7", "reason": "Bias too high in Texas"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deployment_state"] == "rejected"
    assert body["decisions"][0]["decision"] == "rejected"
    assert deployment.admin.promoted == []


# ---------------------------------------------------------------------------
# Forecast hand-off
# ---------------------------------------------------------------------------


def test_dataset_history_is_returned_as_recorded(client):
    dataset = ready_dataset(client)
    body = client.get(f"/v1/training/datasets/{dataset['id']}/history", params={"store_id": "TX_1", "days": 30}).json()
    assert len(body["history"]) == 30
    assert body["history"][-1] == {"date": "2015-07-19", "sales": 1000.0 + 100 + 199}
    missing = client.get(f"/v1/training/datasets/{dataset['id']}/history", params={"store_id": "WI_9"})
    assert missing.status_code == 422


# ---------------------------------------------------------------------------
# Airflow client
# ---------------------------------------------------------------------------


def test_the_client_authenticates_refreshes_and_sends_a_logical_date_free_run():
    calls = []
    tokens = iter(["t1", "t2"])

    def transport(method, url, body, headers, timeout):
        calls.append((method, url, body, headers.get("Authorization")))
        if url.endswith("/auth/token"):
            return 201, {"access_token": next(tokens)}
        if len([c for c in calls if c[0] == "POST" and "dagRuns" in c[1]]) == 1:
            return 401, {"detail": "expired"}
        return 200, {"dag_run_id": "training__x", "state": "queued"}

    settings = SimpleNamespace(
        airflow_api_url="http://api-server:8080",
        airflow_username="svc",
        airflow_password="secret",
        airflow_timeout_seconds=5.0,
        airflow_public_url="",
    )
    client = AirflowClient(settings, transport=transport)
    run = client.trigger("sales_forecast_training", "training__x", {"training_request_id": "x"})
    assert run["state"] == "queued"
    token_calls = [c for c in calls if c[1].endswith("/auth/token")]
    assert token_calls[0][2] == {"username": "svc", "password": "secret"}
    assert len(token_calls) == 2  # refreshed once after the 401
    posted = [c for c in calls if "dagRuns" in c[1]][-1]
    assert posted[2] == {"dag_run_id": "training__x", "logical_date": None, "conf": {"training_request_id": "x"}}
    assert posted[3] == "Bearer t2"


def test_a_409_from_airflow_is_a_duplicate_not_a_failure():
    def transport(method, url, body, headers, timeout):
        if url.endswith("/auth/token"):
            return 201, {"access_token": "t"}
        return 409, {"detail": "exists"}

    settings = SimpleNamespace(airflow_api_url="http://a", airflow_username="u", airflow_password="p", airflow_timeout_seconds=1.0, airflow_public_url="")
    with pytest.raises(AirflowConflict):
        AirflowClient(settings, transport=transport).trigger("d", "r", {})


# ---------------------------------------------------------------------------
# Ingestion through the real upload endpoint
# ---------------------------------------------------------------------------


def rossmann_csv(stores=(1, 2), days=200, gap_store=None) -> bytes:
    rows = []
    for store in stores:
        for day in pd.date_range("2015-01-01", periods=days):
            if gap_store == store and pd.Timestamp("2015-03-01") <= day <= pd.Timestamp("2015-03-05"):
                continue
            open_ = 0 if day.dayofweek == 6 else 1
            rows.append(
                {"Store": store, "DayOfWeek": day.dayofweek + 1, "Date": day.strftime("%Y-%m-%d"),
                 "Sales": (4000 + store + day.day) * open_, "Customers": 400 * open_, "Open": open_,
                 "Promo": 0, "StateHoliday": "0", "SchoolHoliday": 0}
            )
    return pd.DataFrame(rows).to_csv(index=False).encode()


def upload_files(client, files, headers=None, **form):
    data = {"name": f"ds-{uuid.uuid4().hex[:8]}", "mode": "independent", "source_type": "real"}
    data.update(form)
    return client.post(
        "/v1/training/datasets",
        files=[("files", (name, io.BytesIO(content), "text/csv")) for name, content in files],
        data=data,
        headers=headers or {},
    )


def test_the_upload_endpoint_adapts_rossmann_before_validating(client, store):
    response = upload_files(client, [("train.csv", rossmann_csv())], currency="EUR")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "validated", body["validation_report"]["errors"]
    assert body["adapter_id"] == "rossmann" and body["source_namespace"] == "rossmann"
    assert body["stores"] == ["rossmann_1", "rossmann_2"]
    report = body["validation_report"]
    assert report["adapter_message"].startswith("Rossmann data was adapted")
    assert report["rows_received"] == 400 and report["rows_valid"] == 400
    # Normalized output and retained context are stored beside the untouched original.
    keys = [key for key in store.objects if key.startswith(f"datasets/{body['id']}/")]
    assert any("/source/" in key for key in keys)
    assert any("/processed/" in key for key in keys)
    assert any("/context/" in key for key in keys)
    original = next(key for key in keys if "/source/" in key)
    assert store.objects[original] == rossmann_csv()


def test_multiple_related_files_are_one_session(client):
    store_csv = b"Store,StoreType,Assortment,CompetitionDistance,Promo2\n1,a,a,100,0\n2,b,c,200,1\n"
    body = upload_files(client, [("train.csv", rossmann_csv()), ("store.csv", store_csv)], currency="EUR").json()
    assert body["status"] == "validated"
    assert [item["name"] for item in body["source_files"]] == ["train.csv", "store.csv"]
    join = body["validation_report"]["store_metadata"]
    assert join["rows_before"] == join["rows_after"] == 400


def test_a_missing_confirmation_is_asked_for_then_mapping_changes_invalidate(client):
    first = upload_files(client, [("train.csv", rossmann_csv())]).json()
    assert first["status"] == "needs_mapping"
    assert first["validation_report"]["rows_valid"] is None
    assert client.post(f"/v1/training/datasets/{first['id']}/validate").status_code == 409

    configured = client.put(
        f"/v1/training/datasets/{first['id']}/configuration",
        json={"config": {"currency": "EUR"}, "expected_mapping_version": first["mapping_version"]},
    ).json()
    assert configured["status"] == "validated" and configured["mapping_version"] == 2

    changed = client.put(
        f"/v1/training/datasets/{first['id']}/configuration",
        json={"config": {"currency": "UNKNOWN"}, "expected_mapping_version": 2},
    ).json()
    assert changed["mapping_version"] == 3
    # Sealing what the user saw at version 2 is refused: it is stale.
    stale = client.post(f"/v1/training/datasets/{first['id']}/validate", json={"expected_mapping_version": 2})
    assert stale.status_code == 409
    fresh = client.post(f"/v1/training/datasets/{first['id']}/validate", json={"expected_mapping_version": 3})
    assert fresh.json()["status"] == "ready"
    # A ready version cannot be re-mapped.
    again = client.put(f"/v1/training/datasets/{first['id']}/configuration", json={"config": {"currency": "EUR"}})
    assert again.status_code == 422


def test_warnings_that_need_acknowledgment_block_sealing_until_acknowledged(client):
    body = upload_files(
        client, [("train.csv", rossmann_csv(stores=(1, 2, 3), gap_store=2))], currency="EUR",
        config='{"missing_dates": "exclude_stores"}',
    ).json()
    assert body["status"] == "validated"
    assert body["stores"] == ["rossmann_1", "rossmann_3"]
    refused = client.post(f"/v1/training/datasets/{body['id']}/validate")
    assert refused.status_code == 422
    sealed = client.post(
        f"/v1/training/datasets/{body['id']}/validate", json={"acknowledged_warnings": ["STORES_EXCLUDED"]}
    ).json()
    assert sealed["status"] == "ready"
    assert sealed["acknowledged_warnings"] == ["STORES_EXCLUDED"]


def test_large_uploads_are_processed_in_the_background(client, monkeypatch):
    from api.services import ingestion_jobs

    monkeypatch.setenv("TRAINING_INLINE_PROCESSING_BYTES", "0")
    queued = []
    monkeypatch.setattr(ingestion_jobs, "submit", lambda dataset_id, factory: queued.append((dataset_id, factory)))
    response = upload_files(client, [("train.csv", rossmann_csv())], currency="EUR")
    assert response.status_code == 202
    assert response.json()["status"] == "processing"
    # The worker runs later, in its own session; the browser polls the row.
    dataset_id, factory = queued[0]
    ingestion_jobs.run(dataset_id, factory)
    assert client.get(f"/v1/training/datasets/{response.json()['id']}").json()["status"] == "validated"


def test_a_storage_outage_is_a_retryable_failure_not_invalid_data(client, store, monkeypatch):
    original = store.download

    def broken(key, path):
        raise storage.StorageUnavailableError("minio down")

    store.download = broken
    body = upload_files(client, [("train.csv", rossmann_csv())], currency="EUR").json()
    assert body["status"] == "failed"
    assert body["processing_error"].startswith("STORAGE_UNAVAILABLE")
    assert body["validation_report"]["errors"][0]["code"] == "STORAGE_UNAVAILABLE"
    store.download = original
    retried = client.post(f"/v1/training/datasets/{body['id']}/retry").json()
    assert retried["status"] == "validated"


def test_jobs_interrupted_by_a_restart_become_retryable_failures(client):
    from api.services.ingestion_jobs import INTERRUPTED_MESSAGE, recover_interrupted

    body = upload_files(client, [("train.csv", rossmann_csv())], currency="EUR").json()
    with session_scope() as session:
        from api.db.models.training import TrainingDataset

        session.get(TrainingDataset, uuid.UUID(body["id"])).status = "processing"
    assert recover_interrupted() >= 1
    after = client.get(f"/v1/training/datasets/{body['id']}").json()
    assert after["status"] == "failed" and after["processing_error"] == INTERRUPTED_MESSAGE


def test_a_retried_upload_with_the_same_key_is_one_session(client, store):
    first = upload_files(client, [("train.csv", rossmann_csv())], currency="EUR", headers={"Idempotency-Key": "upload-key-0001"})
    stored = len(store.objects)
    second = upload_files(client, [("train.csv", rossmann_csv())], currency="EUR", headers={"Idempotency-Key": "upload-key-0001"})
    assert first.json()["id"] == second.json()["id"]
    assert second.status_code == 200 and len(store.objects) == stored


def test_per_file_and_file_count_limits_are_explained(client, store, monkeypatch):
    monkeypatch.setenv("TRAINING_UPLOAD_MAX_FILE_BYTES", "1000")
    too_big = upload_files(client, [("train.csv", rossmann_csv())], currency="EUR")
    assert too_big.status_code == 413
    assert "limit" in too_big.json()["error"]["message"] and "ZIP" in too_big.json()["error"]["message"]
    monkeypatch.setenv("TRAINING_UPLOAD_MAX_FILE_BYTES", str(10 * 1024 * 1024))
    monkeypatch.setenv("TRAINING_UPLOAD_MAX_FILES", "1")
    too_many = upload_files(client, [("a.csv", b"x"), ("b.csv", b"y")], currency="EUR")
    assert too_many.status_code == 413
    assert store.objects == {}


def test_a_needs_mapping_or_invalid_session_cannot_start_training(client):
    pending = upload_files(client, [("train.csv", rossmann_csv())]).json()
    assert start(client, pending["id"]).status_code == 409


def test_the_contract_publishes_limits_and_adapters(client):
    body = client.get("/v1/training/contract").json()
    assert body["limits"]["max_upload_bytes"] >= 38 * 1024 * 1024
    assert {item["id"] for item in body["adapters"]} == {"canonical_store_day", "rossmann", "mapped_tabular"}
    assert body["serving_currencies"] == ["USD"]
