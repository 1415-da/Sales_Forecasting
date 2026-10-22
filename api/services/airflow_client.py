"""
Server-side client for the Airflow 3 REST API.

Airflow 3 serves ``/api/v2`` and issues bearer tokens from ``/auth/token``
(the SimpleAuthManager and FAB both implement it). This client asks for a token
with the configured service credentials, retries once on a 401 with a fresh
token, and maps every failure to one of three outcomes the caller must handle
differently:

* ``AirflowUnavailable``   -- no answer (connection refused, DNS, 5xx)
* ``AirflowTimeout``       -- no answer *in time*; the request may have landed
* ``AirflowRejected``      -- an answer that says no (auth, missing DAG, 4xx)

The browser never talks to Airflow and never sees these credentials.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from api.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: ``(method, url, body, headers, timeout) -> (status, parsed json)``
Transport = Callable[[str, str, Optional[Dict[str, Any]], Dict[str, str], float], Tuple[int, Any]]


class AirflowError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class AirflowUnavailable(AirflowError):
    """Airflow did not answer."""


class AirflowTimeout(AirflowError):
    """Airflow did not answer in time; a write may or may not have applied."""


class AirflowRejected(AirflowError):
    """Airflow answered with a refusal."""


class AirflowConflict(AirflowRejected):
    """The resource already exists (a dag run with this id)."""


def _urllib_transport(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]],
    headers: Dict[str, str],
    timeout: float,
) -> Tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"detail": raw[:300].decode("utf-8", "replace")}
        return exc.code, payload
    except (socket.timeout, TimeoutError) as exc:
        raise AirflowTimeout(f"Airflow did not respond within {timeout:.0f}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise AirflowTimeout(f"Airflow did not respond within {timeout:.0f}s") from exc
        raise AirflowUnavailable(f"Airflow is unreachable: {exc.reason}") from exc
    except OSError as exc:
        raise AirflowUnavailable(f"Airflow is unreachable: {exc}") from exc


def _detail(payload: Any) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("title") or payload
        if isinstance(detail, list):
            return "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
        return str(detail)
    return str(payload)


class AirflowClient:
    """Typed calls against one Airflow deployment."""

    def __init__(self, settings: Optional[Settings] = None, transport: Optional[Transport] = None) -> None:
        self.settings = settings or get_settings()
        self._transport = transport or _urllib_transport
        self._token: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return self.settings.airflow_api_url

    # -- auth --------------------------------------------------------------

    def _fetch_token(self) -> str:
        status, payload = self._transport(
            "POST",
            f"{self.base_url}/auth/token",
            {"username": self.settings.airflow_username, "password": self.settings.airflow_password},
            {},
            self.settings.airflow_timeout_seconds,
        )
        if status >= 500:
            raise AirflowUnavailable(f"Airflow token endpoint failed ({status})", status)
        if status not in (200, 201) or not isinstance(payload, dict) or "access_token" not in payload:
            raise AirflowRejected(
                "Airflow refused the service credentials. Check AIRFLOW_API_USERNAME / "
                "AIRFLOW_API_PASSWORD.",
                status,
            )
        return str(payload["access_token"])

    def _token_value(self, refresh: bool = False) -> str:
        with self._lock:
            if refresh or not self._token:
                self._token = self._fetch_token()
            return self._token

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        url = f"{self.base_url}{path}"
        for attempt in (0, 1):
            token = self._token_value(refresh=attempt == 1)
            status, payload = self._transport(
                method,
                url,
                body,
                {"Authorization": f"Bearer {token}"},
                self.settings.airflow_timeout_seconds,
            )
            if status == 401 and attempt == 0:
                continue
            if status >= 500:
                raise AirflowUnavailable(f"Airflow error {status}: {_detail(payload)}", status)
            return status, payload
        raise AirflowRejected("Airflow rejected the service token.", 401)

    # -- DAGs and runs -----------------------------------------------------

    @staticmethod
    def _quote(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def get_dag(self, dag_id: str) -> Dict[str, Any]:
        status, payload = self._call("GET", f"/api/v2/dags/{self._quote(dag_id)}")
        if status == 404:
            raise AirflowRejected(f"The DAG '{dag_id}' does not exist in Airflow.", 404)
        if status != 200:
            raise AirflowRejected(f"Airflow refused to describe '{dag_id}': {_detail(payload)}", status)
        return payload

    def trigger(self, dag_id: str, dag_run_id: str, conf: Dict[str, Any], note: str = "") -> Dict[str, Any]:
        """
        Create a dag run with a caller-chosen id.

        The id is deterministic per training request, so a retry after a
        timeout either creates the run or hits ``AirflowConflict`` -- it can
        never create a second one.
        """
        body: Dict[str, Any] = {"dag_run_id": dag_run_id, "logical_date": None, "conf": conf}
        if note:
            body["note"] = note[:1000]
        status, payload = self._call("POST", f"/api/v2/dags/{self._quote(dag_id)}/dagRuns", body)
        if status in (200, 201):
            return payload
        if status == 409:
            raise AirflowConflict(f"Dag run {dag_run_id} already exists.", 409)
        if status == 404:
            raise AirflowRejected(f"The DAG '{dag_id}' does not exist in Airflow.", 404)
        raise AirflowRejected(f"Airflow refused to start the run: {_detail(payload)}", status)

    def get_run(self, dag_id: str, dag_run_id: str) -> Optional[Dict[str, Any]]:
        """The dag run, or ``None`` if Airflow has never heard of it."""
        status, payload = self._call(
            "GET", f"/api/v2/dags/{self._quote(dag_id)}/dagRuns/{self._quote(dag_run_id)}"
        )
        if status == 404:
            return None
        if status != 200:
            raise AirflowRejected(f"Airflow refused to describe the run: {_detail(payload)}", status)
        return payload

    def task_instances(self, dag_id: str, dag_run_id: str) -> List[Dict[str, Any]]:
        status, payload = self._call(
            "GET",
            f"/api/v2/dags/{self._quote(dag_id)}/dagRuns/{self._quote(dag_run_id)}/taskInstances",
        )
        if status == 404:
            return []
        if status != 200:
            raise AirflowRejected(f"Airflow refused to list tasks: {_detail(payload)}", status)
        return list(payload.get("task_instances") or [])

    def xcom(self, dag_id: str, dag_run_id: str, task_id: str, key: str = "return_value") -> Optional[Any]:
        """
        A task's XCom value, or ``None`` when absent.

        Requested without ``deserialize``: Airflow refuses that flag unless
        XCom deserialization is enabled, and a JSON-serialisable return value
        comes back as JSON without it.
        """
        status, payload = self._call(
            "GET",
            f"/api/v2/dags/{self._quote(dag_id)}/dagRuns/{self._quote(dag_run_id)}"
            f"/taskInstances/{self._quote(task_id)}/xcomEntries/{self._quote(key)}",
        )
        if status != 200:
            return None
        value = payload.get("value") if isinstance(payload, dict) else None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    def run_url(self, dag_id: str, dag_run_id: str) -> Optional[str]:
        """Browser link to the run, when a public Airflow URL is configured."""
        if not self.settings.airflow_public_url:
            return None
        return (
            f"{self.settings.airflow_public_url}/dags/{self._quote(dag_id)}"
            f"/runs/{self._quote(dag_run_id)}"
        )
