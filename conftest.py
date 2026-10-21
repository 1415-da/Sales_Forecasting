"""
Repo-root test configuration.

Two jobs, both of which exist because the API gained authentication:

1. **A working environment.** Authentication needs a JWT secret and a database;
   without them every test that builds the app would fail during dependency
   resolution rather than testing anything. A temporary SQLite file is used so
   the real schema is exercised without a database server.

2. **A pre-authenticated app.** Every `/v1/*` route now requires a bearer token
   and a capability. Rather than editing several hundred existing assertions to
   thread a token through, ``create_app`` is wrapped so each app a test builds
   has ``get_current_user`` overridden with an ADMIN. Tests that are *about*
   authentication opt out with the ``no_auth_override`` marker and drive the
   real flow.

The override is deliberately at the ``get_current_user`` seam, not at
``require_permission``: the permission checks still run for real against the
injected user's role, so a test can assert a 403 simply by injecting a
lower-privileged role.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Iterator, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Set before any application module is imported: `api.core.security` reads the
# secret lazily, but a module-level import that touches settings would
# otherwise cache an unconfigured state.
os.environ.setdefault(
    "JWT_SECRET_KEY", "test-only-jwt-secret-key-at-least-32-chars-long"
)
# A low bcrypt cost: the production factor would dominate the suite's runtime
# for no added coverage.
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("SKIP_ADMIN_BOOTSTRAP", "true")

_TEST_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="sf-tests-"), "test.db")
os.environ.setdefault("SCENARIO_DATABASE_URL", f"sqlite+pysqlite:///{_TEST_DB_PATH}")


def pytest_configure(config: pytest.Config) -> None:
    """Register the markers this configuration defines."""
    config.addinivalue_line(
        "markers",
        "no_auth_override: build the app without an injected user, to test the "
        "real authentication flow.",
    )


@pytest.fixture(scope="session", autouse=True)
def _database_schema() -> Iterator[None]:
    """Create the full schema once for the session."""
    import api.db.models  # noqa: F401  populates Base.metadata
    from api.db.session import create_all, reset_engine

    reset_engine()
    create_all()
    yield
    reset_engine()


def make_test_user(role: Optional[object] = None, **overrides: object) -> object:
    """
    An unsaved ``User`` for injection.

    Unsaved on purpose: most tests only need an identity with a role, and
    inserting a row would couple them to the user table's state.
    """
    from api.core.rbac import Role
    from api.db.base import utcnow
    from api.db.models.user import User

    chosen = role or Role.ADMIN
    role_value = chosen.value if hasattr(chosen, "value") else str(chosen)
    now = utcnow()

    user = User(
        id=overrides.pop("id", uuid.uuid4()),
        email=overrides.pop("email", f"{role_value.lower()}@test.local"),
        full_name=overrides.pop("full_name", f"Test {role_value}"),
        hashed_password="x",
        role=role_value,
        is_active=bool(overrides.pop("is_active", True)),
        must_change_password=bool(overrides.pop("must_change_password", False)),
    )
    user.created_at = now
    user.updated_at = now
    user.failed_login_count = 0
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


@pytest.fixture
def test_user():
    """Factory for an injectable user of any role."""
    return make_test_user


@pytest.fixture(autouse=True)
def _authenticated_app(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """
    Wrap ``create_app`` so every app a test builds is authenticated.

    The wrapper is installed on ``api.main`` *and* on every already-imported
    test module that holds a direct reference to the original function --
    ``from api.main import create_app`` binds the name at import time, so
    patching only the source module would have no effect.
    """
    if request.node.get_closest_marker("no_auth_override"):
        yield None
        return

    from api import main as api_main
    from api.db.session import get_session, get_session_factory
    from api.dependencies.auth import get_current_user

    original = api_main.create_app
    default_user = make_test_user()

    def _session_override():
        """
        A real session, so audit writes inside routes actually work.

        The exception is re-raised after the rollback. Swallowing it here would
        suppress the route's own error and FastAPI would then fail with "No
        response object was returned" instead of rendering the 404 or 409 the
        test is asserting.
        """
        session = get_session_factory()()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def wrapped(*args, **kwargs):
        app = original(*args, **kwargs)
        app.dependency_overrides[get_current_user] = lambda: wrapped.current_user
        app.dependency_overrides[get_session] = _session_override
        return app

    wrapped.current_user = default_user  # type: ignore[attr-defined]

    monkeypatch.setattr(api_main, "create_app", wrapped)
    for module in list(sys.modules.values()):
        if getattr(module, "create_app", None) is original:
            monkeypatch.setattr(module, "create_app", wrapped, raising=False)

    yield wrapped


@pytest.fixture
def as_role(_authenticated_app):
    """
    Switch the injected user's role mid-test.

    Lets one test assert that a capability is granted to one role and refused
    to another without rebuilding the app.
    """

    def apply(role: object, **overrides: object):
        user = make_test_user(role, **overrides)
        if _authenticated_app is not None:
            _authenticated_app.current_user = user
        return user

    return apply
