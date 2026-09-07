"""The single async SQLAlchemy engine + session factory this app's DB-backed
stores share (SessionStore, DocumentStore, HitlService, SkillPackageStore,
SkillRunService — see app/services.py::CopilotService.__init__, the one
composition root that constructs this once and threads a session factory
into each store's constructor).

Postgres is mandatory once this module is imported from the app's real
startup path — there is no file-backed fallback (see docs/database.md and
the approved plan's "Postgres becomes mandatory" decision). A missing
`DATABASE_URL` fails loudly at construction time rather than silently
degrading, matching quality.md's "fix obvious startup/import/runtime
errors before completion" and this being a hard requirement now.
"""
from __future__ import annotations

import asyncio
import logging
import socket

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

logger = logging.getLogger(__name__)


def _patch_ipv4_preference() -> None:
    """Patches asyncio.BaseEventLoop.getaddrinfo at the CLASS level (not on
    one loop instance) so an IPv4 address is always tried before any IPv6
    one, on every event loop this process ever creates — uvicorn's,
    pytest-asyncio's, Alembic's, whichever runs build_engine()'s eventual
    connection.

    Exists because asyncpg (via asyncio's own loop.create_connection)
    otherwise dials whichever address getaddrinfo returns first, and some
    environments this app runs in (verified live: a sandboxed dev
    container) have DNS that returns a Neon host's IPv6 address first while
    genuinely being unable to route IPv6 at all — the connection attempt
    then hangs until asyncpg's own connect timeout finally gives up, rather
    than falling back to the perfectly reachable IPv4 address getaddrinfo
    also returned in the same result set.

    A class-level patch (not an instance-level one applied inside an
    already-running loop) is the only version of this fix that actually
    works: SQLAlchemy's own "connect" event fires too late to help — by
    the time it runs, the DBAPI connection attempt that hangs has already
    started (verified live: patching there still hung). This has to be in
    place before asyncpg's own connect ever calls loop.getaddrinfo, and a
    module-level call at import time (see below) is the only point that's
    reliably "before," regardless of which event loop ends up running the
    actual connection.

    Idempotent — checked via a class attribute so importing this module
    twice, or calling build_engine more than once in a process, never
    double-wraps the real getaddrinfo."""
    if getattr(asyncio.BaseEventLoop, "_prefers_ipv4_patched", False):
        return
    original_getaddrinfo = asyncio.BaseEventLoop.getaddrinfo

    async def _ipv4_first_getaddrinfo(self, *args, **kwargs):
        results = await original_getaddrinfo(self, *args, **kwargs)
        return sorted(results, key=lambda r: 0 if r[0] == socket.AF_INET else 1)

    asyncio.BaseEventLoop.getaddrinfo = _ipv4_first_getaddrinfo
    asyncio.BaseEventLoop._prefers_ipv4_patched = True


# Applied at import time, not inside build_engine() — see this function's
# own docstring on why it must be in place before the first connection
# attempt on whatever event loop ends up running it, which build_engine()
# being called synchronously at app startup can't guarantee on its own.
_patch_ipv4_preference()


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when DATABASE_URL is empty at engine-construction time. The
    message is safe to show as-is (no secrets) — see routes' exception
    handler in app/main.py, which never leaks raw exceptions to a client,
    but this one is also safe to log/print directly during startup."""


def build_engine(database_url: str) -> AsyncEngine:
    if not database_url:
        raise DatabaseNotConfiguredError(
            "DATABASE_URL is not set. A Postgres connection string is required to start "
            "this app (see docs/database.md) — e.g. "
            "postgresql+asyncpg://user:password@host:5432/dbname"
        )
    # echo=False always: SQLAlchemy's own query-echo logging can include bound
    # parameter values, which may carry PII/secrets (e.g. password_hash
    # inserts) — never enabled, per security.md.
    return create_async_engine(database_url, echo=False, pool_pre_ping=True)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_all(engine: AsyncEngine) -> None:
    """Creates every table in app/db/models.py if it doesn't already exist —
    used by the test suite's SQLite engine (see conftest.py) and by a bare
    local dev run against a fresh database. Production/staging schema
    changes go through Alembic migrations (alembic/), not this function —
    this is a convenience for a from-scratch database, not a substitute for
    `alembic upgrade head`."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
