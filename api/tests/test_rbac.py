"""
Role-based access control: the matrix, and its enforcement at the HTTP layer.

Two halves, deliberately:

* the matrix itself, asserted against the specification table so a grant
  cannot drift silently;
* real requests through the app as each role, so the enforcement is tested
  rather than the table.

The second half matters more. A correct matrix that no route consults is worth
nothing, and hiding a button in the frontend is not access control.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.core.rbac import (
    ROLE_ORDER,
    Permission,
    Role,
    has_permission,
    matrix_rows,
    permission_names,
    permissions_for,
)
from api.db.models.audit import AuditAction
from api.db.repositories.audit_repository import AuditRepository
from api.db.session import session_scope
from api.main import create_app

# The permission matrix exactly as specified. Written out rather than derived,
# so a change to the implementation has to be mirrored here deliberately.
SPEC = {
    "view_forecast":        {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 1, "BUSINESS_USER": 1, "VIEWER": 1},
    "generate_forecast":    {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 1, "BUSINESS_USER": 1, "VIEWER": 0},
    "run_scenario":         {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 1, "BUSINESS_USER": 1, "VIEWER": 0},
    "export_forecast":      {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 1, "BUSINESS_USER": 1, "VIEWER": 0},
    "view_model_performance": {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 1, "BUSINESS_USER": 1, "VIEWER": 1},
    "view_model_registry":  {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 1, "BUSINESS_USER": 1, "VIEWER": 1},
    "promote_model":        {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 0, "BUSINESS_USER": 0, "VIEWER": 0},
    "rollback_model":       {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 0, "BUSINESS_USER": 0, "VIEWER": 0},
    "archive_model":        {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 0, "BUSINESS_USER": 0, "VIEWER": 0},
    "trigger_retraining":   {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 0, "BUSINESS_USER": 0, "VIEWER": 0},
    "view_monitoring":      {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 1, "BUSINESS_USER": 1, "VIEWER": 1},
    "manage_users":         {"ADMIN": 1, "ML_ENGINEER": 0, "ANALYST": 0, "BUSINESS_USER": 0, "VIEWER": 0},
    "view_audit_logs":      {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 0, "BUSINESS_USER": 0, "VIEWER": 0},
    # The "Limited" cells in the specification are modelled as a separate,
    # finer-grained capability rather than a weaker version of the same one.
    "view_model_registry_detail": {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 1, "BUSINESS_USER": 0, "VIEWER": 0},
    "view_monitoring_detail":     {"ADMIN": 1, "ML_ENGINEER": 1, "ANALYST": 1, "BUSINESS_USER": 0, "VIEWER": 0},
}


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", sorted(SPEC))
def test_matrix_matches_the_specification(capability):
    """Every specified cell is what the code grants."""
    permission = Permission(capability)
    for role_name, expected in SPEC[capability].items():
        role = Role(role_name)
        assert has_permission(role, permission) is bool(expected), (
            f"{role_name} / {capability}"
        )


def test_admin_holds_every_permission():
    """
    Computed, not listed.

    A new permission must not be silently absent from the administrator role.
    """
    assert permissions_for(Role.ADMIN) == frozenset(Permission)


def test_roles_are_strictly_nested_in_privilege():
    """
    Each role's permissions are a superset of the one below it.

    Not required by the specification, but true of it -- and a break would
    mean someone senior had lost a capability a junior role still has, which
    is almost always a mistake.
    """
    for higher, lower in zip(ROLE_ORDER, ROLE_ORDER[1:]):
        assert permissions_for(lower) <= permissions_for(higher), (
            f"{lower.value} is not a subset of {higher.value}"
        )


def test_viewer_cannot_change_anything():
    """A read-only role holds no capability whose name implies a write."""
    write_verbs = ("generate", "run", "promote", "archive", "rollback", "trigger", "manage")
    for name in permission_names(Role.VIEWER):
        assert not any(name.startswith(verb) for verb in write_verbs), name


def test_unknown_role_grants_nothing():
    """
    An unrecognised stored role fails closed.

    A role that has been renamed or corrupted must deny, not default to
    something permissive.
    """
    assert permissions_for(None) == frozenset()
    assert Role.from_value("SUPERUSER") is None
    assert has_permission(Role.from_value("nonsense"), Permission.VIEW_FORECAST) is False


def test_role_parsing_is_case_insensitive():
    """`admin` and `ADMIN` are the same role."""
    assert Role.from_value("admin") is Role.ADMIN
    assert Role.from_value(" Ml_Engineer ") is Role.ML_ENGINEER


def test_matrix_rows_cover_every_capability_and_role():
    """The served matrix is complete, so a rendered table cannot omit a cell."""
    rows = matrix_rows()
    assert len(rows) == len(list(Permission))
    for row in rows:
        for role in ROLE_ORDER:
            assert role.value in row


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


HISTORY = [
    {"date": f"2026-08-{day:02d}", "sales": 3000.0 + day}
    for day in range(1, 32)
] + [
    {"date": f"2026-09-{day:02d}", "sales": 3100.0 + day}
    for day in range(1, 30)
]

FORECAST_BODY = {
    "store_id": "CA_1",
    "model": "ensemble",
    "model_version": "latest",
    "horizon_days": 7,
    "confidence_level": 0.95,
    "history": HISTORY,
}

#: One request per capability, with the role that must be refused. Each entry
#: is a route that genuinely exercises the permission, not a stand-in.
REFUSALS = [
    ("generate_forecast", "VIEWER", "POST", "/v1/forecast", FORECAST_BODY),
    ("manage_users", "ML_ENGINEER", "GET", "/v1/admin/users", None),
    ("manage_users", "ANALYST", "GET", "/v1/admin/users", None),
    ("manage_users", "VIEWER", "GET", "/v1/admin/users", None),
    ("view_audit_logs", "ANALYST", "GET", "/v1/admin/audit-logs", None),
    ("view_audit_logs", "BUSINESS_USER", "GET", "/v1/admin/audit-logs", None),
    ("promote_model", "ANALYST", "POST", "/v1/models/1/promote", {"confirm": True}),
    ("promote_model", "BUSINESS_USER", "POST", "/v1/models/1/promote", {"confirm": True}),
    ("archive_model", "VIEWER", "POST", "/v1/models/1/archive", {"confirm": True}),
    ("rollback_model", "ANALYST", "POST", "/v1/models/1/rollback", {"confirm": True}),
    ("view_monitoring_detail", "BUSINESS_USER", "GET", "/v1/monitoring/drift", None),
    ("view_monitoring_detail", "VIEWER", "GET", "/v1/monitoring/drift", None),
    ("manage_alerts", "ANALYST", "POST",
     "/v1/monitoring/alerts/00000000-0000-0000-0000-000000000000/acknowledge", None),
    ("view_model_registry_detail", "VIEWER", "GET", "/v1/models/1/lineage", None),
]


@pytest.fixture
def client():
    """A client over the real app, with the injected user swappable per test."""
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "capability,role,method,path,body",
    REFUSALS,
    ids=[f"{role}-cannot-{cap}" for cap, role, *_ in REFUSALS],
)
def test_a_role_without_the_capability_is_refused(
    client, as_role, capability, role, method, path, body
):
    """
    403, in the documented envelope, naming what was required.

    Asserted at the HTTP layer because that is where it has to hold: a correct
    matrix that a route never consults protects nothing.
    """
    as_role(Role(role))
    response = client.request(method, path, json=body)

    assert response.status_code == 403, f"{role} reached {path}"
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "PERMISSION_DENIED"
    assert payload["error"]["message"] == (
        "You do not have permission to perform this action."
    )
    details = payload["error"]["details"]
    required = details.get("required") or details.get("required_any_of") or []
    assert capability in required
    assert details["your_role"] == role


@pytest.mark.parametrize("role", ["ADMIN", "ML_ENGINEER", "ANALYST", "BUSINESS_USER", "VIEWER"])
def test_every_role_may_read_the_model_list(client, as_role, role):
    """
    The registry list is readable by everyone, per the specification.

    Asserted for all five roles so a tightening of this route cannot pass
    unnoticed.
    """
    as_role(Role(role))
    response = client.get("/v1/models")
    assert response.status_code != 403


@pytest.mark.parametrize("role", ["ADMIN", "ML_ENGINEER", "ANALYST", "BUSINESS_USER", "VIEWER"])
def test_every_role_may_read_the_monitoring_summary(client, as_role, role):
    """High-level health is visible to every role, including VIEWER."""
    as_role(Role(role))
    response = client.get("/v1/monitoring/summary")
    assert response.status_code != 403


def test_an_unrecognised_stored_role_is_refused_not_defaulted(client, as_role):
    """
    A corrupt role denies rather than silently granting the base set.

    `get_current_user` fails closed on an unparseable role, so a renamed or
    damaged value cannot become an implicit permission grant.
    """
    as_role("NOT_A_ROLE")
    response = client.get("/v1/models")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_a_denial_is_recorded_in_the_audit_log(client, as_role):
    """
    A refused privileged attempt is kept.

    The record is written in its own transaction: the request session rolls
    back when the 403 is raised, and a row written into that session would be
    discarded with it -- which is exactly how denials were going missing.
    """
    as_role(Role.ANALYST, email="denied-probe@example.com")
    response = client.post("/v1/models/1/promote", json={"confirm": True})
    assert response.status_code == 403

    with session_scope() as session:
        entries, _ = AuditRepository(session).search(
            actions=[AuditAction.PERMISSION_DENIED.value], limit=50
        )
    mine = [e for e in entries if e.user_email == "denied-probe@example.com"]
    assert mine, "the denial was not recorded"
    assert mine[0].outcome == "denied"
    assert mine[0].event_metadata["required_permission"] == "promote_model"
    assert mine[0].event_metadata["actor_role"] == "ANALYST"


def test_an_unauthenticated_request_is_401_not_403(client):
    """
    "Who are you" and "you may not" are different answers.

    A client that gets 401 should retry with credentials; one that gets 403
    should not.
    """
    app = create_app()
    # No injected user: exercise the real dependency.
    app.dependency_overrides.clear()
    with TestClient(app, raise_server_exceptions=False) as bare:
        for path in ("/v1/models", "/v1/admin/users", "/v1/monitoring/summary"):
            response = bare.get(path)
            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"


def test_public_routes_stay_public(client):
    """Health and the auth endpoints must not require a token."""
    app = create_app()
    app.dependency_overrides.clear()
    with TestClient(app, raise_server_exceptions=False) as bare:
        assert bare.get("/health", params={"deep": "false"}).status_code == 200
        assert bare.get("/auth/config").status_code == 200
