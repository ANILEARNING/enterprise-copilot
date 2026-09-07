"""Login/signup flow — combines app/auth.py's pure crypto (password
hashing, JWT sign/verify) with app/db/tenancy_repository.py's queries and
app/db/engine.py's session factory. This is the composition layer neither
of those modules owns on its own: app/auth.py has no DB dependency, and
the repository layer has no opinion on what a "signup" or "login" actually
does — this file is where those decisions live.

Also home to get_current_user, the FastAPI dependency a route uses to
require a logged-in caller — built and exported here, but not yet applied
to any existing /api/* route (chat, sessions, documents, ...) in this pass;
see this module's own docstring note below and the routes that DO use it
(app/routes.py's /api/auth/* endpints) for the one place it's wired in so far.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .auth import (
    AccessTokenClaims, InvalidTokenError, create_access_token, generate_refresh_token,
    hash_password, verify_access_token, verify_password,
)
from .config import settings
from .db.models import UserApprovalRow
from .db.tenancy_repository import (
    RefreshTokenRepository, TenantRepository, UserApprovalRepository, UserRepository, UserTenantRepository,
)

_tenants = TenantRepository()
_users = UserRepository()
_approvals = UserApprovalRepository()
_memberships = UserTenantRepository()
_tokens = RefreshTokenRepository()


class AuthError(Exception):
    """Raised for any signup/login failure a route should turn into a 4xx.
    One exception type, like InvalidTokenError in app/auth.py — a login
    route responds the same way ("Incorrect email or password") whether the
    email doesn't exist or the password is wrong, so callers never need to
    distinguish failure reasons via exception type; `message` is the exact
    user-facing text."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _slugify(text: str) -> str:
    """URL/id-safe slug for a new tenant — see Tenant.slug's docstring in
    app/db/models.py. Lowercased, non-alphanumerics collapsed to a single
    hyphen, leading/trailing hyphens trimmed. Not guaranteed unique on its
    own (see _unique_tenant_slug, which appends a suffix on collision)."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "workspace"


async def _unique_tenant_slug(session: AsyncSession, base: str) -> str:
    """Appends -2, -3, ... on a slug collision — tenants.slug is UNIQUE
    (app/db/models.py), so signup can't just retry the same slug and hope;
    this makes the second "Acme" signup get "acme-2" rather than failing
    outright over a cosmetic collision."""
    slug = _slugify(base)
    candidate = slug
    suffix = 2
    while await _tenants.get_by_slug(session, candidate) is not None:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


class AuthResult:
    """What signup()/login() hand back to the route: the access token to
    return to the client, the refresh token (also returned, and the ONLY
    time the raw value ever exists outside this call — see
    RefreshTokenRow's docstring), and enough user/tenant info for the
    response body without a second query."""

    __slots__ = ("access_token", "refresh_token", "user_id", "tenant_id", "email", "approval_status", "platform_role")

    def __init__(
        self, *, access_token: str, refresh_token: str, user_id: uuid.UUID, tenant_id: uuid.UUID,
        email: str, approval_status: str, platform_role: str,
    ):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.email = email
        self.approval_status = approval_status
        self.platform_role = platform_role


async def _issue_login_session(
    session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID, platform_role: str,
    user_agent: str | None, ip_address: str | None,
) -> tuple[str, str]:
    """Issues one (access_token, refresh_token) pair and persists the
    refresh token's record — the one piece of state every login/signup
    success path needs, factored out so signup() and login() don't
    duplicate it."""
    access_token = create_access_token(user_id=user_id, tenant_id=tenant_id, platform_role=platform_role)
    raw_refresh_token = generate_refresh_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    await _tokens.create(
        session, user_id=user_id, tenant_id=tenant_id, raw_token=raw_refresh_token, expires_at=expires_at,
        user_agent=user_agent, ip_address=ip_address,
    )
    return access_token, raw_refresh_token


async def signup(
    session: AsyncSession, *, email: str, password: str, display_name: str | None,
    workspace_name: str | None, user_agent: str | None = None, ip_address: str | None = None,
) -> AuthResult:
    """Creates a new User + a fresh Tenant they own, then logs them in
    immediately (returns a working token pair) — see this repo's approved
    design: every signup creates its own tenant rather than joining an
    existing one (no "invite to a tenant" flow exists yet).

    Bootstrap: if this is the very first user on the whole deployment (the
    users table is empty), the account is created already approved
    (approval_status="active") and as a platform superadmin — otherwise
    there would be no admin able to approve the first account at all. Every
    signup after that stays "pending" per the schema's default, requiring
    an existing superadmin to approve them (see docs/auth.md's approval
    flow this sets up for — not built in this pass) before they can log in.
    """
    if await _users.get_by_email(session, email) is not None:
        raise AuthError("An account with that email already exists.", status_code=409)

    is_first_user = not await _users.any_exist(session)

    user = await _users.create(
        session, email=email, password_hash=hash_password(password), display_name=display_name,
    )
    tenant = await _tenants.create(
        session, slug=await _unique_tenant_slug(session, workspace_name or email.split("@")[0]),
        name=workspace_name or f"{display_name or email}'s workspace",
    )
    if is_first_user:
        now = datetime.now(timezone.utc)
        # Both repository calls fetch-then-mutate the same identity-mapped
        # `user` object created just above (see UserRepository.
        # set_platform_role/set_approval_status's own docstrings) — no
        # separate "keep the in-memory object in sync" step needed here,
        # `user.platform_role`/`user.approval_status` are already correct
        # by the time these two calls return.
        await _users.set_platform_role(session, user.id, role="superadmin")
        await _users.set_approval_status(
            session, user.id, status="active", approved_by_user_id=user.id, approved_at=now,
        )
    # A tenant's OWNER membership is always active the moment they create
    # it, regardless of the user's platform-level approval_status (checked
    # separately below, right before a token is issued) — nothing else
    # exists in a brand-new tenant yet to protect from its own creator.
    await _memberships.create(session, user_id=user.id, tenant_id=tenant.id, role="owner", status="active")
    await session.commit()

    if user.approval_status != "active":
        # Account created, but not yet usable — no token issued. The route
        # (POST /api/auth/signup) still returns 201 with this exact message
        # so the frontend can show "check back once an admin approves you"
        # rather than treating account creation itself as having failed.
        raise AuthError(
            "Account created — waiting for an administrator to approve it before you can sign in.",
            status_code=202,
        )

    access_token, raw_refresh_token = await _issue_login_session(
        session, user_id=user.id, tenant_id=tenant.id, platform_role=user.platform_role,
        user_agent=user_agent, ip_address=ip_address,
    )
    await session.commit()
    return AuthResult(
        access_token=access_token, refresh_token=raw_refresh_token, user_id=user.id, tenant_id=tenant.id,
        email=user.email, approval_status=user.approval_status, platform_role=user.platform_role,
    )


async def login(
    session: AsyncSession, *, email: str, password: str, tenant_id: uuid.UUID | None = None,
    user_agent: str | None = None, ip_address: str | None = None,
) -> AuthResult:
    """Verifies email+password and issues a fresh token pair. `tenant_id`
    picks which of the user's memberships becomes the token's active tenant
    (see create_access_token's docstring) — omitted, the earliest-joined
    membership is used (matches signup's "your own tenant" being the
    obvious default for a user who's never switched workspaces). Raises
    AuthError with the SAME message for "no such email" and "wrong
    password" — never reveal which one it was (see AuthError's docstring)."""
    user = await _users.get_by_email(session, email)
    if user is None or user.password_hash is None or not verify_password(password, user.password_hash):
        raise AuthError("Incorrect email or password.", status_code=401)
    if user.approval_status != "active":
        # Deliberately specific here (unlike the email/password check above)
        # — a real account whose existence is already confirmed by a
        # correct password isn't a secret worth protecting the same way;
        # the user genuinely needs to know WHY they can't log in.
        raise AuthError(f"This account is {user.approval_status} and cannot sign in yet.", status_code=403)

    memberships = await _memberships.list_tenants_for_user(session, user.id)
    active_memberships = [m for m in memberships if m.status == "active"]
    if tenant_id is not None:
        membership = next((m for m in active_memberships if m.tenant_id == tenant_id), None)
        if membership is None:
            raise AuthError("You don't have an active membership in that workspace.", status_code=403)
    elif active_memberships:
        membership = min(active_memberships, key=lambda m: m.created_at)
    else:
        raise AuthError("This account has no active workspace membership.", status_code=403)

    access_token, raw_refresh_token = await _issue_login_session(
        session, user_id=user.id, tenant_id=membership.tenant_id, platform_role=user.platform_role,
        user_agent=user_agent, ip_address=ip_address,
    )
    await session.commit()
    return AuthResult(
        access_token=access_token, refresh_token=raw_refresh_token, user_id=user.id,
        tenant_id=membership.tenant_id, email=user.email, approval_status=user.approval_status,
        platform_role=user.platform_role,
    )


async def refresh_access_token(session: AsyncSession, *, raw_refresh_token: str) -> str:
    """Exchanges a still-valid refresh token for a fresh access token —
    does NOT rotate/replace the refresh token itself (a simpler model than
    refresh-token rotation; revisit if this deployment's threat model wants
    single-use refresh tokens later). Raises AuthError if the token is
    unknown, revoked, or expired."""
    row = await _tokens.get_by_token(session, raw_refresh_token)
    if row is None or row.revoked_at is not None:
        raise AuthError("Refresh token is invalid or has expired.", status_code=401)
    # SQLite (the test suite's engine — see conftest.py/tests/test_auth.py)
    # doesn't preserve tzinfo on a DateTime(timezone=True) column the way
    # Postgres does: a row read back here can be naive even though it was
    # written aware (verified live — comparing it straight against
    # datetime.now(timezone.utc) raises TypeError, not a wrong answer).
    # Normalizing to UTC-aware here defends this comparison on any backend,
    # not just the one this app runs in production.
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise AuthError("Refresh token is invalid or has expired.", status_code=401)
    user = await _users.get(session, row.user_id)
    if user is None or user.approval_status != "active":
        raise AuthError("This account can no longer sign in.", status_code=403)
    return create_access_token(user_id=user.id, tenant_id=row.tenant_id, platform_role=user.platform_role)


async def logout(session: AsyncSession, *, raw_refresh_token: str) -> None:
    """Ends one login session (one device/browser) — revokes only the
    presented refresh token, leaving any others (other devices) untouched.
    Idempotent: revoking an already-revoked or unknown token is a no-op,
    not an error — a logout call should never fail from the client's
    perspective."""
    await _tokens.revoke(session, raw_refresh_token, revoked_at=datetime.now(timezone.utc))
    await session.commit()


# --- FastAPI dependency: require a logged-in caller ---------------------------
#
# Not applied to any existing route in this pass (see module docstring) —
# built and ready for app/routes.py's /api/auth/me (the one route that DOES
# use it) and for future gating of chat/session/document routes, a separate
# decision from building auth itself.

_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure_session_factory(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Called once at app startup (see app/services.py's composition root)
    to give get_current_user a way to open its own DB session — a FastAPI
    dependency can't receive constructor arguments the way CopilotService's
    other DB-backed pieces do, so this module-level setter is the seam
    instead. Mirrors how app/observability.py's `tracer` singleton is
    configured once at import/startup and used by name everywhere after."""
    global _session_factory
    _session_factory = session_factory


async def get_current_user(authorization: str | None = Header(default=None)) -> AccessTokenClaims:
    """FastAPI dependency: `Depends(get_current_user)` on any route that
    should require a valid access token. Reads `Authorization: Bearer
    <token>`, verifies it (see app/auth.py:verify_access_token — signature
    + expiry, no DB round trip), and returns its claims. Raises
    HTTPException(401) for anything wrong, with a header-appropriate detail
    message (never leaks *why* — see InvalidTokenError's docstring)."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return verify_access_token(token)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


async def require_superadmin(claims: AccessTokenClaims = Depends(get_current_user)) -> AccessTokenClaims:
    """FastAPI dependency for the admin approval routes below —
    `Depends(require_superadmin)` composes on top of get_current_user (a
    valid token is still required first) and additionally rejects anyone
    whose token doesn't carry platform_role="superadmin". Safe to trust the
    token's own platform_role claim without a DB re-check: the token is
    signed (see create_access_token), not client-editable, and a
    demoted/suspended user's existing tokens are still only valid for
    jwt_expire_minutes (15 minutes, see app/config.py) before they'd need a
    refresh that re-reads their current User.approval_status anyway (see
    refresh_access_token above)."""
    if claims.platform_role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required.")
    return claims


# --- Admin: approve / reject / suspend / reinstate other users ----------------
#
# Every function below is meant to run behind require_superadmin — none of
# them re-check the caller's own role themselves, matching how the routes
# that call these (app/routes.py's /api/admin/* endpoints) are expected to
# always carry Depends(require_superadmin), same separation of concerns as
# GuardrailService not re-validating auth (app/services.py) — that's a
# route-layer concern, not this module's.

async def list_pending_users(session: AsyncSession):
    """Every account currently awaiting approval — the admin queue's main
    view. Returns User rows directly (not a DTO) — app/routes.py's own
    response_model does the public-shape narrowing, same pattern the rest
    of this app uses (e.g. RAGStore.public(), app/services.py)."""
    return await _users.list_by_approval_status(session, "pending")


async def decide_user_approval(
    session: AsyncSession, *, user_id: uuid.UUID, decided_by_user_id: uuid.UUID, action: str,
    tenant_id: uuid.UUID | None = None, reason: str | None = None,
) -> None:
    """The one function behind approve/reject/suspend/reinstate — differs
    only in `action` and which approval_status it maps to. Writes BOTH the
    cached status on User (set_approval_status) AND the audit row
    (UserApprovalRepository.record) in the same transaction, exactly the
    pairing UserRepository.set_approval_status's own docstring says must
    never happen alone. Raises AuthError(404) for an unknown user_id, or
    AuthError(400) for an unrecognized action — a route calling this with a
    hardcoded, known-good `action` should never actually hit that second
    case; it exists so a bad request body fails loudly instead of writing
    a bogus audit-log action string."""
    status_by_action = {"approved": "active", "rejected": "rejected", "suspended": "suspended", "reinstated": "active"}
    if action not in status_by_action:
        raise AuthError(f"Unknown action {action!r}.", status_code=400)

    user = await _users.get(session, user_id)
    if user is None:
        raise AuthError("No such user.", status_code=404)

    now = datetime.now(timezone.utc)
    await _users.set_approval_status(
        session, user_id, status=status_by_action[action], approved_by_user_id=decided_by_user_id, approved_at=now,
    )
    await _approvals.record(
        session, user_id=user_id, decided_by_user_id=decided_by_user_id, action=action,
        tenant_id=tenant_id, reason=reason,
    )
    if action in ("suspended", "rejected"):
        # A suspended/rejected account's existing sessions must not keep
        # working for up to jwt_expire_minutes after the decision — revoke
        # every refresh token now so no device can silently renew past
        # this point; the short-lived access token they might still be
        # holding expires on its own shortly after (see require_superadmin's
        # docstring on why that residual window is accepted, not "fixed").
        await _tokens.revoke_all_for_user(session, user_id, revoked_at=now)
    await session.commit()


async def get_approval_history(session: AsyncSession, user_id: uuid.UUID) -> list[UserApprovalRow]:
    """Full decision history for one user — an admin's "why is this account
    in the state it's in" audit view."""
    return await _approvals.list_for_user(session, user_id)
