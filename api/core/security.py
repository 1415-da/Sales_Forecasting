"""
Password hashing and JWT issuing/verification.

Pure cryptographic and encoding concerns only: no database, no HTTP, no user
model. That keeps it unit-testable in isolation and keeps the token format in
one auditable place.

Choices worth stating
---------------------
* **bcrypt directly**, not passlib. One fewer dependency, and passlib's bcrypt
  backend currently warns against bcrypt 4.x version detection; calling bcrypt
  is simpler than suppressing that.
* **Separate token types.** Access and refresh tokens carry a ``typ`` claim and
  are rejected when used in the wrong place. Without it, a long-lived refresh
  token would be accepted as a bearer token on every endpoint, which erases the
  point of having a short access-token lifetime.
* **Refresh tokens are opaque to the database.** The JWT carries a ``jti``; the
  server stores only a SHA-256 of the token string. A database leak therefore
  does not yield usable tokens, and revocation is a row update.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import bcrypt
import jwt

from api.core.errors import TokenExpiredError, TokenInvalidError, WeakPasswordError

logger = logging.getLogger(__name__)

JWT_ALGORITHM = "HS256"
DEFAULT_ISSUER = "sales-forecasting-api"
DEFAULT_AUDIENCE = "sales-forecasting"

#: bcrypt's own hard limit: it silently truncates beyond 72 bytes, so a longer
#: password would appear to work while only its first 72 bytes mattered.
BCRYPT_MAX_PASSWORD_BYTES = 72

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

#: Rejected outright, whatever else the policy says.
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "changeme",
        "letmein",
        "admin",
        "administrator",
        "qwertyuiop",
        "iloveyou",
        "welcome1",
        "sales-forecasting",
        "forecasting",
    }
)


class TokenType(str, Enum):
    """What a token may be used for."""

    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True)
class TokenClaims:
    """The verified contents of a token."""

    subject: str
    token_type: TokenType
    role: Optional[str]
    email: Optional[str]
    jti: str
    issued_at: datetime
    expires_at: datetime

    @property
    def user_id(self) -> uuid.UUID:
        """The subject parsed as a user id."""
        return uuid.UUID(self.subject)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """
    Hash a password with bcrypt, returning the encoded hash.

    The cost factor comes from ``BCRYPT_ROUNDS`` so it can be raised as
    hardware improves without a code change, and lowered in tests where a
    production cost would dominate the runtime.
    """
    _assert_hashable(password)
    rounds = _bcrypt_rounds()
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    """
    Check a password against a stored hash.

    Returns False rather than raising for a missing or malformed hash: a caller
    checking credentials wants a boolean, and an exception here would leak the
    difference between "no such user" and "wrong password".
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(
            password.encode("utf-8")[:BCRYPT_MAX_PASSWORD_BYTES],
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError) as exc:
        logger.warning("Rejecting a malformed password hash: %s", type(exc).__name__)
        return False


def validate_password_strength(password: str, *, email: Optional[str] = None) -> None:
    """
    Enforce the password policy, raising ``WeakPasswordError`` with reasons.

    All failures are collected and reported together: telling someone their
    password is too short, only for the next attempt to fail on a missing
    digit, is a bad experience and encourages weaker choices.
    """
    problems: List[str] = []

    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(f"must be at most {MAX_PASSWORD_LENGTH} characters")
    if len(password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        problems.append(
            f"must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes once encoded"
        )
    if not re.search(r"[a-z]", password):
        problems.append("must contain a lowercase letter")
    if not re.search(r"[A-Z]", password):
        problems.append("must contain an uppercase letter")
    if not re.search(r"\d", password):
        problems.append("must contain a digit")
    if not re.search(r"[^A-Za-z0-9]", password):
        problems.append("must contain a symbol")

    collapsed = re.sub(r"[^a-z0-9]", "", password.lower())
    if collapsed in _COMMON_PASSWORDS:
        problems.append("is too common")

    if email:
        local_part = email.split("@", 1)[0].lower()
        if len(local_part) >= 4 and local_part in password.lower():
            problems.append("must not contain your email address")

    if problems:
        raise WeakPasswordError(
            "The password does not meet the requirements: " + "; ".join(problems) + ".",
            field="password",
            details={"requirements": problems},
        )


def generate_password(length: int = 20) -> str:
    """
    Generate a strong random password that satisfies the policy.

    Used for admin-initiated resets and the bootstrap admin, so a temporary
    credential is never a predictable string.
    """
    alphabet = "abcdefghijkmnopqrstuvwxyz"
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"
    symbols = "!@#$%^&*-_=+?"
    pool = alphabet + upper + digits + symbols

    while True:
        candidate = "".join(secrets.choice(pool) for _ in range(max(length, MIN_PASSWORD_LENGTH)))
        try:
            validate_password_strength(candidate)
            return candidate
        except WeakPasswordError:
            # Rejected by the policy only if a character class happened to be
            # absent; draw again rather than patching the string.
            continue


def _assert_hashable(password: str) -> None:
    """Reject a password bcrypt would silently truncate."""
    if not password:
        raise WeakPasswordError("A password is required.", field="password")
    if len(password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
        raise WeakPasswordError(
            f"The password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes "
            "once encoded.",
            field="password",
        )


def _bcrypt_rounds() -> int:
    """Cost factor, bounded to a sane range."""
    try:
        rounds = int(os.getenv("BCRYPT_ROUNDS", "12"))
    except ValueError:
        rounds = 12
    return max(4, min(rounds, 16))


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def jwt_secret() -> str:
    """
    The signing secret.

    Required in any deployment. A generated fallback would mean every restart
    silently invalidated all sessions, and two replicas would reject each
    other's tokens -- failures that are hard to attribute. Failing loudly at
    the first token operation is better.
    """
    secret = os.getenv("JWT_SECRET_KEY") or os.getenv("SECRET_KEY")
    if not secret or not secret.strip():
        raise TokenInvalidError(
            "The API is not configured for authentication.",
            details={"missing_env_var": "JWT_SECRET_KEY"},
        )
    if len(secret.strip()) < 32:
        logger.warning(
            "JWT_SECRET_KEY is shorter than 32 characters; use a longer secret."
        )
    return secret.strip()


def access_token_ttl() -> timedelta:
    """Access-token lifetime, default 45 minutes."""
    return timedelta(minutes=_env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 45, 1, 24 * 60))


def refresh_token_ttl() -> timedelta:
    """Refresh-token lifetime, default 7 days."""
    return timedelta(days=_env_int("REFRESH_TOKEN_EXPIRE_DAYS", 7, 1, 365))


def token_issuer() -> str:
    """The ``iss`` claim."""
    return os.getenv("JWT_ISSUER", DEFAULT_ISSUER)


def token_audience() -> str:
    """The ``aud`` claim."""
    return os.getenv("JWT_AUDIENCE", DEFAULT_AUDIENCE)


def create_token(
    *,
    subject: str,
    token_type: TokenType,
    role: Optional[str] = None,
    email: Optional[str] = None,
    expires_in: Optional[timedelta] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> tuple[str, TokenClaims]:
    """
    Mint a signed token, returning it alongside its claims.

    The claims come back so the caller can persist the ``jti`` and expiry
    without decoding what it just encoded.
    """
    now = datetime.now(timezone.utc)
    ttl = expires_in or (
        access_token_ttl() if token_type is TokenType.ACCESS else refresh_token_ttl()
    )
    expires_at = now + ttl
    jti = uuid.uuid4().hex

    payload: Dict[str, Any] = {
        "sub": subject,
        "typ": token_type.value,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": token_issuer(),
        "aud": token_audience(),
    }
    if role:
        payload["role"] = role
    if email:
        payload["email"] = email
    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, jwt_secret(), algorithm=JWT_ALGORITHM)
    claims = TokenClaims(
        subject=subject,
        token_type=token_type,
        role=role,
        email=email,
        jti=jti,
        issued_at=now,
        expires_at=expires_at,
    )
    return token, claims


def decode_token(token: str, expected_type: Optional[TokenType] = None) -> TokenClaims:
    """
    Verify a token's signature, expiry, issuer, audience, and type.

    Every failure becomes a typed error, so a route never has to interpret a
    PyJWT exception, and a token of the wrong type is rejected as firmly as a
    forged one.
    """
    try:
        payload = jwt.decode(
            token,
            jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            issuer=token_issuer(),
            audience=token_audience(),
            options={"require": ["sub", "exp", "iat", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.InvalidTokenError as exc:
        # Covers a bad signature, wrong issuer/audience, and missing claims.
        # The specific reason is logged, never returned: it would tell an
        # attacker which part of a forged token to fix.
        logger.info("Rejected a token: %s", type(exc).__name__)
        raise TokenInvalidError() from exc

    raw_type = str(payload.get("typ", ""))
    try:
        token_type = TokenType(raw_type)
    except ValueError as exc:
        raise TokenInvalidError() from exc

    if expected_type is not None and token_type is not expected_type:
        logger.info(
            "Rejected a %s token where a %s token was required",
            token_type.value,
            expected_type.value,
        )
        raise TokenInvalidError(
            f"A {expected_type.value} token is required for this operation."
        )

    subject = str(payload["sub"])
    try:
        uuid.UUID(subject)
    except (ValueError, AttributeError) as exc:
        raise TokenInvalidError() from exc

    return TokenClaims(
        subject=subject,
        token_type=token_type,
        role=payload.get("role"),
        email=payload.get("email"),
        jti=str(payload["jti"]),
        issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=timezone.utc),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc),
    )


def hash_token(token: str) -> str:
    """
    A stable digest of a token, for storage.

    The refresh-token row holds only this. A database leak then yields no
    usable token, and lookup is still a single indexed equality match.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str, stored_hash: str) -> bool:
    """Constant-time comparison of a token against a stored digest."""
    return hmac.compare_digest(hash_token(token), stored_hash)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """A bounded integer from the environment."""
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("Invalid %s; using %s", name, default)
        return default
    return max(minimum, min(value, maximum))


__all__ = [
    "BCRYPT_MAX_PASSWORD_BYTES",
    "JWT_ALGORITHM",
    "MIN_PASSWORD_LENGTH",
    "TokenClaims",
    "TokenType",
    "access_token_ttl",
    "create_token",
    "decode_token",
    "generate_password",
    "hash_password",
    "hash_token",
    "refresh_token_ttl",
    "token_audience",
    "token_issuer",
    "tokens_match",
    "validate_password_strength",
    "verify_password",
]
