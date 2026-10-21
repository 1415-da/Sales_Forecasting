"""
User administration and the registry mutations.

The guard rails get the most attention. "An administrator cannot lock everyone
out" and "a mutation that silently did not apply is reported as unverified" are
the two properties that turn this from a set of endpoints into something safe
to give someone.
"""

from __future__ import annotations

import uuid

import pytest

from api.core.errors import (
    InvalidStateTransitionError,
    ModelNotFoundError,
    PermissionDeniedError,
)
from api.core.rbac import Role
from api.core.security import hash_password, verify_password
from api.db.models.audit import AuditAction
from api.db.repositories.audit_repository import AuditRepository
from api.db.repositories.user_repository import RefreshTokenRepository
from api.db.session import session_scope
from api.services.audit_service import ActorContext
from api.services.auth_service import AuthService
from api.services.registry_admin_service import (
    STAGE_ARCHIVED,
    STAGE_PRODUCTION,
    RegistryAction,
    RegistryAdminService,
)
from api.services.user_service import UserService

from .conftest import FakeModelVersion, FakeMlflowClient, FakeRun, FakeRunData

STRONG = "Str0ng-Passw0rd!x"


def unique_email(prefix="admin-test"):
    """A fresh address, so tests do not collide on the unique index."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def users():
    """A ``UserService`` over a committed session."""

    class Factory:
        def __enter__(self):
            self._scope = session_scope()
            self.session = self._scope.__enter__()
            return UserService(self.session)

        def __exit__(self, *exc):
            return self._scope.__exit__(*exc)

    return Factory


def admin_actor(user_id=None):
    """Actor context for an administrator."""
    return ActorContext(
        user_id=user_id or uuid.uuid4(),
        email="acting-admin@example.com",
        role=Role.ADMIN,
        ip_address="10.0.0.9",
    )


# ---------------------------------------------------------------------------
# Creating and editing users
# ---------------------------------------------------------------------------


def test_a_created_account_gets_a_generated_password_it_must_change(users):
    """
    The server generates the credential, and it is returned exactly once.

    An administrator inventing a password is worse entropy and one more secret
    to communicate; requiring a change means a credential someone else has
    seen does not stay in use.
    """
    email = unique_email()
    with users() as service:
        user, generated = service.create_user(
            email=email, full_name="New Person", role=Role.ANALYST,
            actor=admin_actor(),
        )
        assert generated is not None and len(generated) >= 12
        assert user.must_change_password is True
        assert user.role == Role.ANALYST.value
        # Stored as a hash, and the generated value verifies against it.
        assert user.hashed_password != generated
        assert verify_password(generated, user.hashed_password) is True


def test_an_explicit_password_must_meet_the_policy(users):
    """A weak administrator-chosen password is refused, not silently accepted."""
    from api.core.errors import WeakPasswordError

    with users() as service:
        with pytest.raises(WeakPasswordError):
            service.create_user(
                email=unique_email(), full_name="X", role=Role.VIEWER,
                password="weak", actor=admin_actor(),
            )


def test_a_partial_edit_leaves_unmentioned_fields_alone(users):
    """A PATCH must not blank out what it did not mention."""
    with users() as service:
        user, _ = service.create_user(
            email=unique_email(), full_name="Original Name",
            role=Role.ANALYST, actor=admin_actor(),
        )
        user_id = user.id
    with users() as service:
        target = service.require_user(str(user_id))
        service.update_user(user=target, actor=admin_actor(), role=Role.VIEWER)
        assert target.full_name == "Original Name"
        assert target.role == Role.VIEWER.value


def test_a_role_change_revokes_the_users_sessions(users):
    """
    Otherwise a demoted user keeps their old permissions until the token expires.

    The change has to take effect now, not eventually.
    """
    email = unique_email()
    with session_scope() as session:
        auth = AuthService(session)
        auth.users.create(
            email=email, full_name="Demote Me",
            hashed_password=hash_password(STRONG), role=Role.ML_ENGINEER,
        )
    with session_scope() as session:
        result = AuthService(session).authenticate(
            email=email, password=STRONG, actor=ActorContext.anonymous()
        )
        user_id = result.user.id

    with session_scope() as session:
        assert len(RefreshTokenRepository(session).active_sessions(user_id)) == 1

    with users() as service:
        target = service.require_user(str(user_id))
        service.update_user(user=target, actor=admin_actor(), role=Role.VIEWER)

    with session_scope() as session:
        assert RefreshTokenRepository(session).active_sessions(user_id) == []


def test_deactivation_revokes_sessions_and_blocks_sign_in(users):
    """A deactivated account is out immediately, not at token expiry."""
    email = unique_email()
    with session_scope() as session:
        AuthService(session).users.create(
            email=email, full_name="Gone",
            hashed_password=hash_password(STRONG), role=Role.ANALYST,
        )
    with session_scope() as session:
        user_id = AuthService(session).authenticate(
            email=email, password=STRONG, actor=ActorContext.anonymous()
        ).user.id

    with users() as service:
        service.set_active(
            user=service.require_user(str(user_id)), active=False, actor=admin_actor()
        )

    with session_scope() as session:
        assert RefreshTokenRepository(session).active_sessions(user_id) == []
    from api.core.errors import AccountInactiveError

    with session_scope() as session:
        with pytest.raises(AccountInactiveError):
            AuthService(session).authenticate(
                email=email, password=STRONG, actor=ActorContext.anonymous()
            )


def test_a_reset_returns_a_new_password_and_clears_any_lockout(users):
    """
    A reset is a way out of a lockout, not another thing to wait for.

    Every session is revoked too: a reset that left old sessions alive would
    not have secured anything.
    """
    email = unique_email()
    with users() as service:
        user, _ = service.create_user(
            email=email, full_name="Reset Me", role=Role.ANALYST, actor=admin_actor()
        )
        service.users.update(user, failed_login_count=9, locked_until=None)
        user_id = user.id
    with users() as service:
        target = service.require_user(str(user_id))
        _, temporary, _ = service.reset_password(user=target, actor=admin_actor())
        assert verify_password(temporary, target.hashed_password) is True
        assert target.must_change_password is True
        assert target.failed_login_count == 0
        assert target.locked_until is None


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_an_admin_cannot_change_their_own_role(users):
    """
    Self-demotion is almost always a mistake.

    Another administrator can do it deliberately; doing it to yourself by
    misclicking a dropdown should not be possible.
    """
    with users() as service:
        user, _ = service.create_user(
            email=unique_email(), full_name="Self", role=Role.ADMIN,
            actor=admin_actor(),
        )
        with pytest.raises(PermissionDeniedError, match="your own account"):
            service.update_user(
                user=user, actor=admin_actor(user_id=user.id), role=Role.VIEWER
            )


def test_an_admin_cannot_deactivate_their_own_account(users):
    """Self-deactivation is always a mistake."""
    with users() as service:
        user, _ = service.create_user(
            email=unique_email(), full_name="Self", role=Role.ADMIN,
            actor=admin_actor(),
        )
        with pytest.raises(PermissionDeniedError):
            service.set_active(
                user=user, active=False, actor=admin_actor(user_id=user.id)
            )


def test_the_last_active_admin_cannot_be_demoted(users):
    """
    One mis-click must not leave a deployment with no way back in.

    Recovering would require direct database access, so the transition is
    refused while no other active administrator exists.
    """
    # Start from a clean slate: deactivate every existing admin so the one
    # created below is genuinely the last.
    with users() as service:
        existing, _ = service.list_users(role=Role.ADMIN, is_active=True, limit=200)
        for account in existing:
            service.users.update(account, is_active=False)

        last, _ = service.create_user(
            email=unique_email("last-admin"), full_name="Last Admin",
            role=Role.ADMIN, actor=admin_actor(),
        )
        with pytest.raises(InvalidStateTransitionError, match="only active administrator"):
            service.update_user(user=last, actor=admin_actor(), role=Role.ANALYST)
        with pytest.raises(InvalidStateTransitionError):
            service.set_active(user=last, active=False, actor=admin_actor())

        # With a second administrator, the transition is allowed.
        service.create_user(
            email=unique_email("second-admin"), full_name="Second Admin",
            role=Role.ADMIN, actor=admin_actor(),
        )
        service.update_user(user=last, actor=admin_actor(), role=Role.ANALYST)
        assert last.role == Role.ANALYST.value


def test_a_role_change_records_both_the_old_and_new_value(users):
    """
    "Changed to ANALYST" is far less useful later than "ADMIN -> ANALYST".

    The audit record has to answer what it was, not only what it became.
    """
    with users() as service:
        user, _ = service.create_user(
            email=unique_email(), full_name="Audited", role=Role.VIEWER,
            actor=admin_actor(),
        )
        user_id, email = user.id, user.email
    with users() as service:
        service.update_user(
            user=service.require_user(str(user_id)),
            actor=admin_actor(),
            role=Role.ANALYST,
        )

    with session_scope() as session:
        entries, _ = AuditRepository(session).search(
            actions=[AuditAction.USER_ROLE_CHANGED.value], limit=50
        )
    mine = [e for e in entries if e.resource_id == str(user_id)]
    assert mine, "the role change was not audited"
    change = mine[0].event_metadata["role"]
    assert change == {"from": "VIEWER", "to": "ANALYST"}
    assert mine[0].event_metadata["email"] == email


def test_an_unknown_user_id_is_a_404_not_a_crash(users):
    """A malformed or stale id is a clean 404."""
    from api.core.errors import UserNotFoundError

    with users() as service:
        with pytest.raises(UserNotFoundError):
            service.require_user("not-a-uuid")
        with pytest.raises(UserNotFoundError):
            service.require_user(str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Registry mutations
# ---------------------------------------------------------------------------


def _registry(settings, versions, applied=None):
    """A ``RegistryAdminService`` over a fake MLflow, recording transitions."""
    from api.services.model_registry_service import ModelRegistryService

    name = settings.registered_model_name("ensemble")
    client = FakeMlflowClient(
        versions={name: versions},
        runs={"run-1": FakeRun(FakeRunData(metrics={"ensemble_rmse": 10.0}))},
    )
    calls = applied if applied is not None else []

    def transport(path, body):
        calls.append((path, body))
        # Apply the transition to the fake registry, so the service's
        # verification step reads a genuinely updated state.
        for version in client.versions[body["name"]]:
            if version.version == body["version"]:
                version.current_stage = body["stage"]
            elif body.get("archive_existing_versions") and (
                version.current_stage == STAGE_PRODUCTION
            ):
                version.current_stage = STAGE_ARCHIVED
        return {}

    service = RegistryAdminService(
        settings=settings,
        registry=ModelRegistryService(settings, client_factory=lambda: client),
        transport=transport,
    )
    # Verification builds its own service; point it at the same fake.
    service._verify_stage = lambda model, version, expected: any(  # noqa: SLF001
        item.version == str(version) and item.current_stage == expected
        for item in client.versions[name]
    )
    return service, client, calls


def test_promotion_archives_the_previously_serving_version(settings):
    """
    Exactly one version serves at a time.

    Two Production versions would make "which model produced this forecast"
    unanswerable, which is worse than a brief gap.
    """
    name = settings.registered_model_name("ensemble")
    versions = [
        FakeModelVersion(name=name, version="1", current_stage=STAGE_PRODUCTION),
        FakeModelVersion(name=name, version="2", current_stage="None"),
    ]
    service, client, calls = _registry(settings, versions)

    result = service.promote(model="ensemble", version="2")

    assert result.action is RegistryAction.PROMOTE
    assert result.new_stage == STAGE_PRODUCTION
    assert result.archived_versions == ["1"]
    assert result.verified is True
    assert calls[0][1]["archive_existing_versions"] is True


def test_promoting_the_serving_version_is_refused(settings):
    """A no-op transition is reported rather than quietly accepted."""
    name = settings.registered_model_name("ensemble")
    service, _, _ = _registry(
        settings,
        [FakeModelVersion(name=name, version="1", current_stage=STAGE_PRODUCTION)],
    )
    with pytest.raises(InvalidStateTransitionError, match="already in Production"):
        service.promote(model="ensemble", version="1")


def test_archiving_the_serving_version_is_refused(settings):
    """
    It would leave the API with nothing to load.

    The message points at the correct move: promote a replacement, which
    archives this one as part of the same transition.
    """
    name = settings.registered_model_name("ensemble")
    service, _, _ = _registry(
        settings,
        [FakeModelVersion(name=name, version="1", current_stage=STAGE_PRODUCTION)],
    )
    with pytest.raises(InvalidStateTransitionError, match="Promote a replacement"):
        service.archive(model="ensemble", version="1")


def test_rollback_requires_something_to_roll_back_from(settings):
    """
    With nothing serving, this is a promotion.

    Calling it a rollback would misdescribe what happened in the audit log.
    """
    name = settings.registered_model_name("ensemble")
    service, _, _ = _registry(
        settings,
        [
            FakeModelVersion(name=name, version="1", current_stage="None"),
            FakeModelVersion(name=name, version="2", current_stage="None"),
        ],
    )
    with pytest.raises(InvalidStateTransitionError, match="Use promote instead"):
        service.rollback(model="ensemble", version="1")


def test_rollback_restores_an_earlier_version_and_archives_the_current(settings):
    """A rollback is a promotion with the intent recorded differently."""
    name = settings.registered_model_name("ensemble")
    service, _, _ = _registry(
        settings,
        [
            FakeModelVersion(name=name, version="1", current_stage=STAGE_ARCHIVED),
            FakeModelVersion(name=name, version="2", current_stage=STAGE_PRODUCTION),
        ],
    )
    result = service.rollback(model="ensemble", version="1")

    assert result.action is RegistryAction.ROLLBACK
    assert result.new_stage == STAGE_PRODUCTION
    assert result.archived_versions == ["2"]
    assert result.verified is True


def test_a_transition_that_does_not_take_effect_is_reported_unverified(settings):
    """
    The failure that once left the registry with no Production version.

    The client accepted the call and reported success while the stage never
    changed. Now the service re-reads the version and says so.
    """
    from api.services.model_registry_service import ModelRegistryService

    name = settings.registered_model_name("ensemble")
    client = FakeMlflowClient(
        versions={
            name: [
                FakeModelVersion(name=name, version="1", current_stage=STAGE_PRODUCTION),
                FakeModelVersion(name=name, version="2", current_stage="None"),
            ]
        },
        runs={"run-1": FakeRun(FakeRunData(metrics={}))},
    )
    service = RegistryAdminService(
        settings=settings,
        registry=ModelRegistryService(settings, client_factory=lambda: client),
        # Accepts the call and changes nothing, like the removed 3.x client did.
        transport=lambda path, body: {},
    )
    service._verify_stage = lambda *a, **k: False  # noqa: SLF001

    result = service.promote(model="ensemble", version="2")
    assert result.verified is False


def test_an_unknown_model_or_version_is_a_404(settings):
    """Both are reported with the available options."""
    name = settings.registered_model_name("ensemble")
    service, _, _ = _registry(
        settings, [FakeModelVersion(name=name, version="1", current_stage="None")]
    )
    with pytest.raises(ModelNotFoundError) as caught:
        service.promote(model="ensemble", version="99")
    assert caught.value.details["available_versions"] == ["1"]

    with pytest.raises(ModelNotFoundError):
        service.promote(model="prophet", version="1")


def test_comparison_direction_decides_what_improved(settings):
    """
    `version_b` is the candidate, so "improved" means the candidate is better.

    Lower-is-better and higher-is-better metrics are judged differently, so R²
    improving is not reported as a regression.
    """
    from api.services.model_registry_service import ModelRegistryService

    name = settings.registered_model_name("ensemble")
    client = FakeMlflowClient(
        versions={
            name: [
                FakeModelVersion(name=name, version="1", run_id="run-a"),
                FakeModelVersion(name=name, version="2", run_id="run-b"),
            ]
        },
        runs={
            "run-a": FakeRun(FakeRunData(metrics={"ensemble_rmse": 100.0, "ensemble_r2": 0.80})),
            "run-b": FakeRun(FakeRunData(metrics={"ensemble_rmse": 80.0, "ensemble_r2": 0.90})),
        },
    )
    service = RegistryAdminService(
        settings=settings,
        registry=ModelRegistryService(settings, client_factory=lambda: client),
        transport=lambda path, body: {},
    )
    comparison = service.compare(model="ensemble", version_a="1", version_b="2")
    verdicts = {row.metric: row.verdict for row in comparison.metrics}

    assert verdicts["rmse"] == "improved"   # 80 < 100, lower is better
    assert verdicts["r2"] == "improved"     # 0.90 > 0.80, higher is better
    assert comparison.improved_count == 2
    assert comparison.regressed_count == 0


def test_a_metric_recorded_for_only_one_version_is_incomparable(settings):
    """
    Not "unchanged", and not silently dropped.

    Treating a missing value as equal would report a real difference as no
    difference.
    """
    from api.services.model_registry_service import ModelRegistryService

    name = settings.registered_model_name("ensemble")
    client = FakeMlflowClient(
        versions={
            name: [
                FakeModelVersion(name=name, version="1", run_id="run-a"),
                FakeModelVersion(name=name, version="2", run_id="run-b"),
            ]
        },
        runs={
            "run-a": FakeRun(FakeRunData(metrics={"ensemble_rmse": 100.0})),
            "run-b": FakeRun(FakeRunData(metrics={"ensemble_wape": 0.1})),
        },
    )
    service = RegistryAdminService(
        settings=settings,
        registry=ModelRegistryService(settings, client_factory=lambda: client),
        transport=lambda path, body: {},
    )
    comparison = service.compare(model="ensemble", version_a="1", version_b="2")

    assert all(row.verdict == "incomparable" for row in comparison.metrics)
    assert comparison.improved_count == 0
    assert "cannot be compared" in comparison.summary


def test_lineage_exposes_provenance_but_no_storage_paths(settings):
    """
    An object-store location is not something a browser should be handed.

    Provenance does not require it, and exposing it would both leak
    infrastructure and invite bypassing the API.
    """
    name = settings.registered_model_name("ensemble")
    service, _, _ = _registry(
        settings, [FakeModelVersion(name=name, version="1", current_stage="None")]
    )
    lineage = service.lineage("ensemble", "1")

    assert lineage["run_id"] == "run-1"
    rendered = repr(lineage)
    for leak in ("s3://", "minio", "/usr/local", "mlflow-artifacts"):
        assert leak not in rendered
