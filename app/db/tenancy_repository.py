"""Repository layer for the tenancy/auth tables (app/db/models.py: Tenant,
User, UserTenant, UserApprovalRow, AuthIdentity, RefreshTokenRow). Nothing in
this app calls these yet — app/tenancy.py (login/signup/JWT issuance) and the
admin approval endpoints are the callers this exists for, not built in this
pass. This file establishes the query surface they'll need, ahead of them.

Deliberately Core-style, matching app/db/models.py's own docstring: explicit
`select()`/`insert()`/`update()` statements against each table, no ORM
relationship traversal, no cascades. Every tenant-scoped table's queries
carry an explicit `WHERE tenant_id = :tenant_id` (see that same docstring's
"Tenant isolation is enforced by convention, not by the database").

One repository class per aggregate the callers will actually reach for
(users+approvals together, since every approval decision touches both;
tenants+memberships together, since a membership is meaningless without its
tenant; refresh tokens on their own, since login-session lifecycle is a
distinct concern) rather than one repository per raw table — matches how a
caller will actually think about these operations, not how the schema
happens to be normalized.

Every method takes an `AsyncSession` as its first argument rather than
owning a session factory itself — the caller (a future app/tenancy.py route
handler, or a request-scoped dependency) controls the transaction boundary,
since an operation like "approve a user" needs UserRepository.set_approval
and UserApprovalRepository.record to commit together or not at all. See
app/db/engine.py's build_session_factory — a caller opens one
`async with session_factory() as session:` block per request and passes
that same `session` to every repository call within it."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuthIdentity, RefreshTokenRow, Tenant, User, UserApprovalRow, UserTenant


def hash_refresh_token(raw_token: str) -> str:
    """SHA-256 hex digest — refresh tokens are already high-entropy random
    values (unlike a user-chosen password), so a fast hash is the right
    tool here, not bcrypt/argon2: the threat this defends against is "a
    stolen DB dump exposes still-usable session tokens," not "an offline
    guessing attack against low-entropy input." Shared as a module function
    (not buried in RefreshTokenRepository) so a caller can hash a
    client-presented token the same way before calling get_by_token_hash,
    without reaching into the repository's internals."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class TenantRepository:
    async def create(self, session: AsyncSession, *, slug: str, name: str) -> Tenant:
        tenant = Tenant(slug=slug, name=name)
        session.add(tenant)
        await session.flush()  # populates tenant.id without requiring the caller to commit first
        return tenant

    async def get(self, session: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
        return await session.get(Tenant, tenant_id)

    async def get_by_slug(self, session: AsyncSession, slug: str) -> Tenant | None:
        result = await session.execute(select(Tenant).where(Tenant.slug == slug))
        return result.scalar_one_or_none()


class UserRepository:
    """Global (non-tenant-scoped) user identity — see User's own docstring
    in app/db/models.py for why platform_role/approval_status live here
    rather than on UserTenant."""

    async def create(
        self, session: AsyncSession, *, email: str, password_hash: str | None = None,
        display_name: str | None = None,
    ) -> User:
        user = User(email=email, password_hash=password_hash, display_name=display_name)
        session.add(user)
        await session.flush()
        return user

    async def get(self, session: AsyncSession, user_id: uuid.UUID) -> User | None:
        return await session.get(User, user_id)

    async def get_by_email(self, session: AsyncSession, email: str) -> User | None:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def any_exist(self, session: AsyncSession) -> bool:
        """Whether ANY user row exists at all, regardless of
        approval_status — the exact question app/tenancy.py:signup needs to
        answer "is this the very first account on this deployment" (see its
        docstring's bootstrap note), which list_by_approval_status can't
        answer correctly on its own (a rejected/suspended-only user table
        would still need a real first-admin bootstrap, not just checking
        pending+active)."""
        result = await session.execute(select(func.count()).select_from(User))
        return result.scalar_one() > 0

    async def list_by_approval_status(self, session: AsyncSession, status: str) -> list[User]:
        """Backs an admin's "pending approvals" queue — the one query this
        table's ix_users_approval_status index (app/db/models.py) exists
        for. Not tenant-scoped, matching User.approval_status itself being
        global; a platform superadmin reviewing this list sees every
        pending user on the deployment, not one tenant's slice of it."""
        result = await session.execute(
            select(User).where(User.approval_status == status).order_by(User.created_at)
        )
        return list(result.scalars().all())

    async def set_approval_status(
        self, session: AsyncSession, user_id: uuid.UUID, *, status: str,
        approved_by_user_id: uuid.UUID, approved_at: datetime,
    ) -> None:
        """Updates the cached "current state" columns only — the caller is
        responsible for also writing a UserApprovalRepository.record() row
        in the SAME session/transaction (see this module's docstring on
        shared transaction boundaries), since those two writes together are
        what "approve a user" actually means; this method alone would leave
        an inconsistent cache with no audit trail behind it.

        Fetches then mutates (not a bulk Core update()) so the ORM's own
        unit-of-work keeps this session's identity map in sync — a bulk
        update() leaves any already-loaded User Python object silently
        stale for the rest of this session (verified live). No-op for an
        unknown user_id."""
        user = await session.get(User, user_id)
        if user is None:
            return
        user.approval_status = status
        user.approved_by_user_id = approved_by_user_id
        user.approved_at = approved_at

    async def set_platform_role(self, session: AsyncSession, user_id: uuid.UUID, *, role: str) -> None:
        user = await session.get(User, user_id)
        if user is None:
            return
        user.platform_role = role


class UserApprovalRepository:
    """Append-only audit log — see UserApprovalRow's docstring in
    app/db/models.py. No update/delete methods: a correction is a new row
    (e.g. action="reinstated"), never an edit to history."""

    async def record(
        self, session: AsyncSession, *, user_id: uuid.UUID, decided_by_user_id: uuid.UUID, action: str,
        tenant_id: uuid.UUID | None = None, reason: str | None = None,
    ) -> UserApprovalRow:
        row = UserApprovalRow(
            user_id=user_id, tenant_id=tenant_id, decided_by_user_id=decided_by_user_id,
            action=action, reason=reason,
        )
        session.add(row)
        await session.flush()
        return row

    async def list_for_user(self, session: AsyncSession, user_id: uuid.UUID) -> list[UserApprovalRow]:
        """Full decision history for one user, oldest first — what a "why
        was this account suspended" audit view reads from."""
        result = await session.execute(
            select(UserApprovalRow).where(UserApprovalRow.user_id == user_id).order_by(UserApprovalRow.created_at)
        )
        return list(result.scalars().all())


class UserTenantRepository:
    """(user, tenant) membership rows — see UserTenant's docstring in
    app/db/models.py. Every method here is tenant_id-scoped except
    list_tenants_for_user, which by definition spans every tenant one user
    belongs to."""

    async def create(
        self, session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID,
        role: str = "member", status: str = "pending", invited_by_user_id: uuid.UUID | None = None,
    ) -> UserTenant:
        membership = UserTenant(
            user_id=user_id, tenant_id=tenant_id, role=role, status=status, invited_by_user_id=invited_by_user_id,
        )
        session.add(membership)
        await session.flush()
        return membership

    async def get(self, session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID) -> UserTenant | None:
        return await session.get(UserTenant, {"user_id": user_id, "tenant_id": tenant_id})

    async def list_members(self, session: AsyncSession, tenant_id: uuid.UUID) -> list[UserTenant]:
        """Every membership row for one tenant — an admin's "people in this
        workspace" list. Ordered by created_at so the earliest members
        (usually the tenant's own founders) sort first."""
        result = await session.execute(
            select(UserTenant).where(UserTenant.tenant_id == tenant_id).order_by(UserTenant.created_at)
        )
        return list(result.scalars().all())

    async def list_pending_for_tenant(self, session: AsyncSession, tenant_id: uuid.UUID) -> list[UserTenant]:
        """Uses ix_user_tenants_tenant_status (app/db/models.py) — a
        tenant admin's own "pending join requests" queue, distinct from
        UserRepository.list_by_approval_status's platform-wide one."""
        result = await session.execute(
            select(UserTenant)
            .where(UserTenant.tenant_id == tenant_id, UserTenant.status == "pending")
            .order_by(UserTenant.created_at)
        )
        return list(result.scalars().all())

    async def list_tenants_for_user(self, session: AsyncSession, user_id: uuid.UUID) -> list[UserTenant]:
        """Every tenant one user belongs to — what a multi-tenant "switch
        workspace" picker reads from after login."""
        result = await session.execute(select(UserTenant).where(UserTenant.user_id == user_id))
        return list(result.scalars().all())

    async def set_status(
        self, session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID, status: str,
    ) -> None:
        """Fetches then mutates — see UserRepository.set_approval_status's
        docstring for why (a bulk update() here would leave an
        already-loaded UserTenant object stale for the rest of this
        session). No-op for an unknown (user_id, tenant_id) pair."""
        membership = await session.get(UserTenant, {"user_id": user_id, "tenant_id": tenant_id})
        if membership is None:
            return
        membership.status = status

    async def set_role(self, session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> None:
        membership = await session.get(UserTenant, {"user_id": user_id, "tenant_id": tenant_id})
        if membership is None:
            return
        membership.role = role


class AuthIdentityRepository:
    """Google/SSO external identities — see AuthIdentity's docstring in
    app/db/models.py. Reserved: no OAuth provider is wired up to call this
    yet, same posture as the model itself."""

    async def create(
        self, session: AsyncSession, *, user_id: uuid.UUID, provider: str, provider_user_id: str,
    ) -> AuthIdentity:
        identity = AuthIdentity(user_id=user_id, provider=provider, provider_user_id=provider_user_id)
        session.add(identity)
        await session.flush()
        return identity

    async def get_by_provider_subject(
        self, session: AsyncSession, *, provider: str, provider_user_id: str,
    ) -> AuthIdentity | None:
        """The actual SSO login lookup: "this provider says this external
        subject just authenticated — which of our users is that?" — uses
        uq_auth_identities_provider_subject (app/db/models.py)."""
        result = await session.execute(
            select(AuthIdentity).where(
                AuthIdentity.provider == provider, AuthIdentity.provider_user_id == provider_user_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user(self, session: AsyncSession, user_id: uuid.UUID) -> list[AuthIdentity]:
        result = await session.execute(select(AuthIdentity).where(AuthIdentity.user_id == user_id))
        return list(result.scalars().all())


class RefreshTokenRepository:
    """Login-session lifecycle — see RefreshTokenRow's docstring in
    app/db/models.py for why only the refresh token (never the access
    token) gets a row here. Every method takes/returns the raw token where
    a caller needs one to compare against a client-presented value, and
    hashes it internally via hash_refresh_token — a caller should never
    need to call that helper directly except when it does the initial
    issuance itself (see create)."""

    async def create(
        self, session: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID, raw_token: str,
        expires_at: datetime, user_agent: str | None = None, ip_address: str | None = None,
    ) -> RefreshTokenRow:
        row = RefreshTokenRow(
            user_id=user_id, tenant_id=tenant_id, token_hash=hash_refresh_token(raw_token),
            expires_at=expires_at, user_agent=user_agent, ip_address=ip_address,
        )
        session.add(row)
        await session.flush()
        return row

    async def get_by_token(self, session: AsyncSession, raw_token: str) -> RefreshTokenRow | None:
        """The actual "is this refresh token still valid" check a token-
        refresh endpoint runs — looks up by hash (see hash_refresh_token),
        never stores or compares the raw value anywhere past this call.
        Does NOT filter out expired/revoked rows itself (that's the
        caller's job, checking .expires_at/.revoked_at on what's returned)
        — a caller distinguishing "token not found" from "token found but
        expired" needs the row either way, not a bool."""
        result = await session.execute(
            select(RefreshTokenRow).where(RefreshTokenRow.token_hash == hash_refresh_token(raw_token))
        )
        return result.scalar_one_or_none()

    async def revoke(self, session: AsyncSession, raw_token: str, *, revoked_at: datetime) -> None:
        """Logout for one specific session/device. Fetches then mutates —
        see UserRepository.set_approval_status's docstring for why (a bulk
        update() would leave an already-loaded RefreshTokenRow, e.g. one
        get_by_token just returned to the caller, silently stale). No-op
        for an unknown token, matching this method's original idempotent-
        logout contract."""
        row = await self.get_by_token(session, raw_token)
        if row is None:
            return
        row.revoked_at = revoked_at

    async def revoke_all_for_user(self, session: AsyncSession, user_id: uuid.UUID, *, revoked_at: datetime) -> None:
        """"Log out everywhere" / forced logout after a password change —
        revokes every still-live session for this user. Loops over fetched
        rows rather than one bulk update() statement — same identity-map
        staleness reasoning as revoke() above, just across N rows instead
        of one; this table's per-user row count is small enough (one
        per logged-in device) that N individual attribute sets cost nothing
        meaningful next to N-times-fewer round trips a bulk statement would
        save."""
        result = await session.execute(
            select(RefreshTokenRow).where(RefreshTokenRow.user_id == user_id, RefreshTokenRow.revoked_at.is_(None))
        )
        for row in result.scalars().all():
            row.revoked_at = revoked_at

    async def list_active_for_user(self, session: AsyncSession, user_id: uuid.UUID) -> list[RefreshTokenRow]:
        """A "your active sessions" account-settings page — every
        not-yet-revoked row, regardless of whether it's also expired (a
        caller wanting only genuinely-usable sessions filters expires_at
        itself; this stays a simple index-backed query via
        ix_refresh_tokens_user_id, app/db/models.py)."""
        result = await session.execute(
            select(RefreshTokenRow).where(
                RefreshTokenRow.user_id == user_id, RefreshTokenRow.revoked_at.is_(None),
            )
        )
        return list(result.scalars().all())
