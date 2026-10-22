"""
Authentication: registration, login, refresh rotation, logout, and lockout.

Driven through the real service against a real (SQLite) database rather than
mocks: the behaviours that matter here -- a rotated token being rejected on
replay, a lockout surviving a process restart -- are properties of persisted
state, and a mocked repository would assert nothing about them.
"""

from __future__ import annotations

import uuid

import pytest

from api.core.errors import (
    AccountInactiveError,
    AccountLockedError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    RegistrationDisabledError,
    RoleNotSelfAssignableError,
    TokenInvalidError,
    WeakPasswordError,
)
from api.core.rbac import Role
from api.core.security import (
    TokenType,
    decode_token,
    hash_password,
    hash_token,
    validate_password_strength,
    verify_password,
)
from api.db.models.audit import AuditAction
from api.db.repositories.audit_repository import AuditRepository
from api.db.repositories.user_repository import RefreshTokenRepository, UserRepository
from api.db.session import session_scope
from api.services.audit_service import ActorContext
from api.services.auth_service import AuthService

STRONG = "Str0ng-Passw0rd!x"
ACTOR = ActorContext.anonymous(ip_address="10.0.0.1", user_agent="pytest", request_id="req_t")


def unique_email(prefix: str = "user") -> str:
    """A fresh address, so tests do not collide on the unique index."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def auth():
    """An ``AuthService`` over a committed session, as a context manager factory."""

    class Factory:
        def __enter__(self):
            self._scope = session_scope()
            self.session = self._scope.__enter__()
            return AuthService(self.session)

        def __exit__(self, *exc):
            return self._scope.__exit__(*exc)

    return Factory


# ---------------------------------------------------------------------------
# Password handling
# ---------------------------------------------------------------------------


def test_password_is_hashed_not_stored():
    """A stored credential must not be the password."""
    digest = hash_password(STRONG)
    assert digest != STRONG
    assert digest.startswith("$2")
    assert verify_password(STRONG, digest) is True
    assert verify_password("wrong", digest) is False


def test_the_same_password_hashes_differently_each_time():
    """Per-hash salting: two accounts with one password must not collide."""
    assert hash_password(STRONG) != hash_password(STRONG)


def test_a_malformed_hash_is_rejected_rather_than_raising():
    """
    A corrupt stored hash fails the check instead of crashing.

    An exception here would leak the difference between "no such user" and
    "wrong password" through the error path.
    """
    assert verify_password(STRONG, "not-a-bcrypt-hash") is False
    assert verify_password(STRONG, None) is False


@pytest.mark.parametrize(
    "password,missing",
    [
        ("short1!A", "at least 12 characters"),
        ("alllowercase1!", "uppercase"),
        ("ALLUPPERCASE1!", "lowercase"),
        ("NoDigitsHere!!", "digit"),
        ("NoSymbolsHere1", "symbol"),
    ],
)
def test_password_policy_names_every_failure(password, missing):
    """
    All failures are reported together.

    Telling someone their password is too short, only for the next attempt to
    fail on a missing digit, encourages weaker choices.
    """
    with pytest.raises(WeakPasswordError) as caught:
        validate_password_strength(password)
    assert any(missing in item for item in caught.value.details["requirements"])


def test_password_may_not_contain_the_email_local_part():
    """A password derived from the address is barely a secret."""
    with pytest.raises(WeakPasswordError, match="requirements"):
        validate_password_strength("Alexandra-2024!", email="alexandra@example.com")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registration_is_refused_when_disabled(auth, monkeypatch):
    """
    Self-registration is closed by default.

    An open sign-up form on a forecasting platform would let anyone reach the
    model.
    """
    monkeypatch.setenv("ALLOW_SELF_REGISTRATION", "false")
    with auth() as service:
        with pytest.raises(RegistrationDisabledError) as caught:
            service.register(
                email=unique_email(), full_name="A", password=STRONG, actor=ACTOR
            )
    assert caught.value.status_code == 403


def test_registration_assigns_the_configured_role_not_a_requested_one(
    auth, monkeypatch
):
    """With no role requested, the configured default is assigned."""
    monkeypatch.setenv("ALLOW_SELF_REGISTRATION", "true")
    monkeypatch.setenv("SELF_REGISTRATION_ROLE", "ANALYST")
    with auth() as service:
        result = service.register(
            email=unique_email(), full_name="Ada", password=STRONG, actor=ACTOR
        )
    assert result.user.role == Role.ANALYST.value


def test_self_registration_cannot_grant_a_privileged_role(auth, monkeypatch):
    """
    A misconfigured privileged default is downgraded, not honoured.

    Self-registration must never be a path to promoting a model.
    """
    monkeypatch.setenv("ALLOW_SELF_REGISTRATION", "true")
    monkeypatch.setenv("SELF_REGISTRATION_ROLE", "ADMIN")
    with auth() as service:
        result = service.register(
            email=unique_email(), full_name="Nope", password=STRONG, actor=ACTOR
        )
    assert result.user.role == Role.VIEWER.value


@pytest.mark.parametrize(
    "role", [Role.VIEWER, Role.BUSINESS_USER, Role.ANALYST, Role.ML_ENGINEER]
)
def test_a_self_assignable_role_chosen_at_sign_up_is_granted(auth, monkeypatch, role):
    monkeypatch.setenv("ALLOW_SELF_REGISTRATION", "true")
    monkeypatch.delenv("SELF_REGISTRATION_ROLES", raising=False)
    with auth() as service:
        result = service.register(
            email=unique_email(), full_name="Ada", password=STRONG, actor=ACTOR, role=role
        )
    assert result.user.role == role.value


def test_admin_cannot_be_chosen_at_sign_up_by_default(auth, monkeypatch):
    """
    A public form that hands out ADMIN hands out the platform.

    The request is refused -- not silently downgraded -- and the refusal is
    audited, so an attempt to self-escalate is visible.
    """
    monkeypatch.setenv("ALLOW_SELF_REGISTRATION", "true")
    monkeypatch.delenv("SELF_REGISTRATION_ROLES", raising=False)
    email = unique_email()
    with auth() as service:
        with pytest.raises(RoleNotSelfAssignableError) as caught:
            service.register(
                email=email, full_name="Eve", password=STRONG, actor=ACTOR, role=Role.ADMIN
            )
    assert caught.value.status_code == 403
    assert "ADMIN" not in caught.value.details["allowed_roles"]
    with session_scope() as session:
        assert UserRepository(session).get_by_email(email) is None


def test_admin_can_be_made_self_assignable_explicitly(auth, monkeypatch):
    """A private demo may opt in; it has to be named in the list to count."""
    monkeypatch.setenv("ALLOW_SELF_REGISTRATION", "true")
    monkeypatch.setenv("SELF_REGISTRATION_ROLES", "VIEWER,ADMIN")
    with auth() as service:
        result = service.register(
            email=unique_email(), full_name="Demo", password=STRONG, actor=ACTOR, role=Role.ADMIN
        )
    assert result.user.role == Role.ADMIN.value


def test_a_role_outside_a_narrowed_list_is_refused(auth, monkeypatch):
    monkeypatch.setenv("ALLOW_SELF_REGISTRATION", "true")
    monkeypatch.setenv("SELF_REGISTRATION_ROLES", "VIEWER")
    monkeypatch.setenv("SELF_REGISTRATION_ROLE", "VIEWER")
    with auth() as service:
        with pytest.raises(RoleNotSelfAssignableError):
            service.register(
                email=unique_email(), full_name="Al", password=STRONG, actor=ACTOR,
                role=Role.ML_ENGINEER,
            )


def test_duplicate_email_is_rejected_case_insensitively(auth, monkeypatch):
    """`Ada@x.com` and `ada@x.com` are one account, not two."""
    monkeypatch.setenv("ALLOW_SELF_REGISTRATION", "true")
    email = unique_email()
    with auth() as service:
        service.register(email=email, full_name="A", password=STRONG, actor=ACTOR)
    with auth() as service:
        with pytest.raises(EmailAlreadyRegisteredError):
            service.register(
                email=email.upper(), full_name="B", password=STRONG, actor=ACTOR
            )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def _make_user(service: AuthService, *, role=Role.ANALYST, active=True) -> str:
    """Create an account directly and return its email."""
    email = unique_email(role.value.lower())
    service.users.create(
        email=email,
        full_name=f"Test {role.value}",
        hashed_password=hash_password(STRONG),
        role=role,
        is_active=active,
    )
    return email


def test_login_returns_a_usable_token_pair(auth):
    """A successful sign-in yields a verifiable access token."""
    with auth() as service:
        email = _make_user(service)
        result = service.authenticate(email=email, password=STRONG, actor=ACTOR)

        claims = decode_token(result.tokens.access_token, TokenType.ACCESS)
        assert claims.user_id == result.user.id
        assert claims.role == Role.ANALYST.value
        assert result.tokens.expires_in > 0
        assert result.user.last_login_at is not None


@pytest.mark.parametrize("wrong_email", [False, True])
def test_wrong_password_and_unknown_email_are_indistinguishable(auth, wrong_email):
    """
    One error for both, so the login form is not an enumeration oracle.

    The same exception type, status, and message whichever half is wrong.
    """
    with auth() as service:
        email = _make_user(service)
        target = unique_email("ghost") if wrong_email else email
        with pytest.raises(InvalidCredentialsError) as caught:
            service.authenticate(email=target, password="definitely-wrong", actor=ACTOR)
    assert caught.value.status_code == 401
    assert caught.value.message == "Incorrect email or password."


def test_a_deactivated_account_cannot_sign_in(auth):
    """Deactivation blocks sign-in even with the correct password."""
    with auth() as service:
        email = _make_user(service, active=False)
        with pytest.raises(AccountInactiveError) as caught:
            service.authenticate(email=email, password=STRONG, actor=ACTOR)
    assert caught.value.status_code == 403


def test_repeated_failures_lock_the_account(auth, monkeypatch):
    """
    The lockout threshold is enforced, and checked before the password.

    Checking it first means hammering a locked account cannot be used to keep
    testing passwords.
    """
    monkeypatch.setenv("MAX_LOGIN_ATTEMPTS", "3")
    with auth() as service:
        email = _make_user(service)

    for _ in range(3):
        with auth() as service:
            with pytest.raises(InvalidCredentialsError):
                service.authenticate(email=email, password="nope", actor=ACTOR)

    with auth() as service:
        with pytest.raises(AccountLockedError) as caught:
            # Even the *correct* password is refused while locked.
            service.authenticate(email=email, password=STRONG, actor=ACTOR)
    assert caught.value.status_code == 423


def test_a_successful_login_clears_the_failure_counter(auth, monkeypatch):
    """A near-miss must not accumulate towards a future lockout."""
    monkeypatch.setenv("MAX_LOGIN_ATTEMPTS", "5")
    with auth() as service:
        email = _make_user(service)
    with auth() as service:
        with pytest.raises(InvalidCredentialsError):
            service.authenticate(email=email, password="nope", actor=ACTOR)
    with auth() as service:
        result = service.authenticate(email=email, password=STRONG, actor=ACTOR)
        assert result.user.failed_login_count == 0
        assert result.user.locked_until is None


# ---------------------------------------------------------------------------
# Refresh rotation
# ---------------------------------------------------------------------------


def test_refresh_rotates_the_token(auth):
    """Each refresh issues a new token and retires the old one."""
    with auth() as service:
        email = _make_user(service)
        first = service.authenticate(email=email, password=STRONG, actor=ACTOR)
        original = first.tokens.refresh_token

    with auth() as service:
        second = service.refresh(refresh_token=original, actor=ACTOR)
    assert second.tokens.refresh_token != original

    with session_scope() as session:
        stored = RefreshTokenRepository(session).get_by_hash(hash_token(original))
        assert stored is not None
        assert stored.is_revoked is True
        assert stored.revoked_reason == "rotated"
        assert stored.replaced_by_jti is not None


def test_replaying_a_rotated_token_revokes_the_whole_session_family(auth):
    """
    Replay is treated as compromise, not as a retry.

    Either an attacker has the old token or the client lost the rotation;
    forcing a fresh sign-in is the safe response to both, so *every* session
    for that user is revoked rather than just the replayed one.
    """
    with auth() as service:
        email = _make_user(service)
        first = service.authenticate(email=email, password=STRONG, actor=ACTOR)
        original = first.tokens.refresh_token

    with auth() as service:
        rotated = service.refresh(refresh_token=original, actor=ACTOR)
        current = rotated.tokens.refresh_token

    with auth() as service:
        with pytest.raises(TokenInvalidError):
            service.refresh(refresh_token=original, actor=ACTOR)

    # The replacement is dead too: the family was revoked.
    with auth() as service:
        with pytest.raises(TokenInvalidError):
            service.refresh(refresh_token=current, actor=ACTOR)


def test_an_access_token_cannot_be_used_to_refresh(auth):
    """
    The token type is enforced.

    Without it a short-lived access token could be exchanged for a fresh pair,
    and vice versa, which erases the point of two lifetimes.
    """
    with auth() as service:
        email = _make_user(service)
        result = service.authenticate(email=email, password=STRONG, actor=ACTOR)
        with pytest.raises(TokenInvalidError):
            service.refresh(refresh_token=result.tokens.access_token, actor=ACTOR)


def test_deactivation_invalidates_an_existing_refresh_token(auth):
    """
    Deactivation takes effect on issued tokens, not only on new sign-ins.

    Active status is re-read from the database, so a token minted before the
    change stops working.
    """
    with auth() as service:
        email = _make_user(service)
        result = service.authenticate(email=email, password=STRONG, actor=ACTOR)
        token = result.tokens.refresh_token

    with session_scope() as session:
        repository = UserRepository(session)
        user = repository.get_by_email(email)
        repository.update(user, is_active=False)

    with auth() as service:
        with pytest.raises(AccountInactiveError):
            service.refresh(refresh_token=token, actor=ACTOR)


# ---------------------------------------------------------------------------
# Logout and password change
# ---------------------------------------------------------------------------


def test_logout_revokes_the_presented_session(auth):
    """The token presented at logout stops working."""
    with auth() as service:
        email = _make_user(service)
        result = service.authenticate(email=email, password=STRONG, actor=ACTOR)
        token = result.tokens.refresh_token
        actor = ActorContext(user_id=result.user.id, email=email, role=Role.ANALYST)

    with auth() as service:
        assert service.logout(refresh_token=token, actor=actor) == 1
    with auth() as service:
        with pytest.raises(TokenInvalidError):
            service.refresh(refresh_token=token, actor=ACTOR)


def test_logout_succeeds_without_a_usable_token(auth):
    """
    Logout always leaves the caller signed out.

    A client that has lost its token still needs the call to work; failing
    would strand it unable to clear its own state.
    """
    with auth() as service:
        assert service.logout(refresh_token=None, actor=ACTOR) == 0
        assert service.logout(refresh_token="garbage", actor=ACTOR) == 0


def test_changing_a_password_revokes_every_session(auth):
    """
    Otherwise the change does not actually lock anyone out.

    This is the whole point of a password change, so it is asserted rather
    than assumed.
    """
    with auth() as service:
        email = _make_user(service)
        first = service.authenticate(email=email, password=STRONG, actor=ACTOR)
        second = service.authenticate(email=email, password=STRONG, actor=ACTOR)
        user = first.user
        new_password = "An0ther-Str0ng!pw"
        service.change_password(
            user=user,
            current_password=STRONG,
            new_password=new_password,
            actor=ActorContext(user_id=user.id, email=email, role=Role.ANALYST),
        )

    for token in (first.tokens.refresh_token, second.tokens.refresh_token):
        with auth() as service:
            with pytest.raises(TokenInvalidError):
                service.refresh(refresh_token=token, actor=ACTOR)

    with auth() as service:
        assert service.authenticate(
            email=email, password="An0ther-Str0ng!pw", actor=ACTOR
        ).user.email == email


def test_a_wrong_current_password_is_refused(auth):
    """Changing a password requires proving you know the old one."""
    with auth() as service:
        email = _make_user(service)
        user = service.users.get_by_email(email)
        with pytest.raises(InvalidCredentialsError):
            service.change_password(
                user=user,
                current_password="not-it",
                new_password="An0ther-Str0ng!pw",
                actor=ACTOR,
            )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_login_and_failure_are_both_audited(auth):
    """
    Both outcomes are recorded, and the password never is.

    A pattern of failures is the signal; a logged password would be a leak.
    """
    with auth() as service:
        email = _make_user(service)
    with auth() as service:
        with pytest.raises(InvalidCredentialsError):
            service.authenticate(email=email, password="nope", actor=ACTOR)
    with auth() as service:
        service.authenticate(email=email, password=STRONG, actor=ACTOR)

    with session_scope() as session:
        entries, _ = AuditRepository(session).search(
            actions=[AuditAction.USER_LOGIN.value, AuditAction.USER_LOGIN_FAILED.value],
            limit=50,
        )
    relevant = [
        entry
        for entry in entries
        if (entry.event_metadata or {}).get("email") == email
        or entry.user_email == email
    ]
    assert any(entry.action == AuditAction.USER_LOGIN.value for entry in relevant)
    assert any(
        entry.action == AuditAction.USER_LOGIN_FAILED.value for entry in relevant
    )
    for entry in relevant:
        assert STRONG not in repr(entry.event_metadata)
        assert "nope" not in repr(entry.event_metadata)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_does_nothing_when_users_exist(auth):
    """
    It only ever runs against an empty table.

    That is what makes it safe on every restart: it cannot overwrite or
    re-privilege an existing account.
    """
    with auth() as service:
        _make_user(service)
    with auth() as service:
        assert service.bootstrap_admin() is None
