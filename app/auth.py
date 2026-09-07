"""Password hashing and JWT access-token sign/verify — the pure-crypto
layer, deliberately with no database dependency of its own (see
app/tenancy.py for the login/signup flow that combines this with
app/db/tenancy_repository.py's queries; keeping the two separate means this
file's correctness is checkable in isolation, no DB fixture needed).

Access tokens are short-lived, stateless JWTs (settings.jwt_expire_minutes —
see app/config.py), verified by signature alone on every request, never
looked up in the database. Refresh tokens are the opposite: long-lived,
opaque random strings with a server-side record (RefreshTokenRow, app/
db/tenancy_repository.py) so they're revocable — see that module's own
docstring for why only the refresh token gets a row.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .config import settings

# bcrypt, not a faster general-purpose hash — a password (unlike a refresh
# token) is chosen by a human and often low-entropy, so the hash needs to be
# deliberately slow against offline guessing. Calls the bcrypt package
# directly rather than through passlib: passlib 1.7.4 (last released 2020,
# unmaintained) breaks against bcrypt>=4.1 (its version-detection reads an
# `__about__` attribute bcrypt itself dropped) — verified live against the
# bcrypt version this repo actually installs. passlib was only ever a thin
# wrapper around this same library here, so removing it drops an abandoned
# dependency instead of pinning around its bug.
#
# bcrypt truncates any input over 72 BYTES silently on some backends (raises
# on others, as hit above) — a real limit worth encoding explicitly rather
# than depending on whichever behavior the installed backend happens to
# have. ChatRequest.message allows up to 20000 chars elsewhere in this app
# (app/models.py) for comparison; a password has no such reason to be long,
# so this is enforced at the Pydantic model layer (see app/models.py:
# SignupRequest), not silently truncated here.
_BCRYPT_MAX_BYTES = 72

# The JWT's own "type of token" claim — belt-and-suspenders against a
# refresh token (opaque random string, never actually a JWT — see
# app/db/tenancy_repository.py) or some other signed token ever being
# accepted here by mistake. Currently only one value exists, but the claim
# is cheap insurance against a future second token type sharing this secret.
_ACCESS_TOKEN_TYPE = "access"


class InvalidTokenError(ValueError):
    """Raised by verify_access_token for any reason a token isn't currently
    usable — expired, bad signature, wrong claims shape, wrong type. One
    exception type rather than PyJWT's several: every route calling this
    wants the same response (401), not different handling per failure mode
    — see docs/auth.md's "never reveal *why* a token was rejected" posture,
    same reasoning check_input/check_output never echo back *why* a message
    was blocked (see GuardrailService, app/services.py)."""


def hash_password(raw_password: str) -> str:
    encoded = raw_password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_BYTES:
        raise ValueError(f"Password must be at most {_BCRYPT_MAX_BYTES} bytes.")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(raw_password: str, password_hash: str) -> bool:
    encoded = raw_password.encode("utf-8")
    if len(encoded) > _BCRYPT_MAX_BYTES:
        return False  # can't possibly match a hash of a <=72-byte password
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("ascii"))
    except ValueError:
        # A malformed/corrupt stored hash — never surface as a crash on a
        # login attempt, same "wrong password" response either way (see
        # InvalidTokenError's docstring on not revealing *why* something failed).
        return False


def generate_refresh_token() -> str:
    """A high-entropy opaque string — not a JWT, carries no claims of its
    own. Only ever compared by hash (see app/db/tenancy_repository.py:
    hash_refresh_token) against RefreshTokenRow.token_hash; its only job is
    being unguessable, not self-describing."""
    return secrets.token_urlsafe(32)


def create_access_token(*, user_id: uuid.UUID, tenant_id: uuid.UUID, platform_role: str) -> str:
    """Issues a signed JWT access token. `tenant_id` is the currently-active
    tenant for this session (a user can belong to more than one — see
    UserTenant, app/db/models.py; switching tenants means issuing a fresh
    token, not mutating this one). `platform_role` is embedded so a route
    can check "is this a superadmin" from the token alone, no DB round trip
    — safe to trust because the token is signed, not client-editable."""
    if not settings.jwt_secret_key:
        raise RuntimeError("JWT_SECRET_KEY is not set — cannot issue access tokens (see app/config.py).")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "platform_role": platform_role,
        "type": _ACCESS_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")


class AccessTokenClaims:
    """The verified, typed result of decoding an access token — routes read
    these attributes rather than raw dict keys, so a claims-shape typo
    fails at the one place this class is constructed, not scattered across
    every route that reads request.state.user.sub or similar."""

    __slots__ = ("user_id", "tenant_id", "platform_role")

    def __init__(self, user_id: uuid.UUID, tenant_id: uuid.UUID, platform_role: str):
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.platform_role = platform_role


def verify_access_token(token: str) -> AccessTokenClaims:
    """Decodes and validates a JWT access token — signature, expiry, and
    the `type` claim (see _ACCESS_TOKEN_TYPE) all checked. Raises
    InvalidTokenError for anything wrong; never returns a partially-valid
    result."""
    if not settings.jwt_secret_key:
        raise RuntimeError("JWT_SECRET_KEY is not set — cannot verify access tokens (see app/config.py).")
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("Invalid or expired token.") from exc
    if payload.get("type") != _ACCESS_TOKEN_TYPE:
        raise InvalidTokenError("Invalid or expired token.")
    try:
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]), tenant_id=uuid.UUID(payload["tenant_id"]),
            platform_role=payload["platform_role"],
        )
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("Invalid or expired token.") from exc
