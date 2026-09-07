"""app/auth.py (pure crypto: password hashing, JWT sign/verify) and
app/tenancy.py (the signup/login/refresh/logout flow that combines it with
app/db/tenancy_repository.py). Each test that touches the DB builds its own
throwaway SQLite engine (mirrors app/db/engine.py:create_all's own "why
SQLite is enough for tests" reasoning) rather than sharing state across
tests — a signup/login test genuinely wants a fresh, empty users table.
"""
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import (
    InvalidTokenError, create_access_token, generate_refresh_token, hash_password,
    verify_access_token, verify_password,
)
from app.config import settings
from app.db.models import Base
from app.tenancy import AuthError, login, logout, refresh_access_token, signup


# --- app/auth.py: pure crypto, no DB ------------------------------------------

def test_hash_password_round_trips():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong password", h) is False


def test_hash_password_rejects_over_72_bytes():
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("x" * 100)


def test_verify_password_over_72_bytes_returns_false_not_raises():
    h = hash_password("a reasonably short password")
    assert verify_password("x" * 100, h) is False


def test_verify_password_against_corrupt_hash_returns_false():
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False


def test_generate_refresh_token_is_unique_and_url_safe():
    a, b = generate_refresh_token(), generate_refresh_token()
    assert a != b
    assert all(c.isalnum() or c in "-_" for c in a)


def test_access_token_round_trips(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret_key", "test-secret-key-at-least-32-bytes-long!!")
    user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    token = create_access_token(user_id=user_id, tenant_id=tenant_id, platform_role="superadmin")
    claims = verify_access_token(token)
    assert claims.user_id == user_id
    assert claims.tenant_id == tenant_id
    assert claims.platform_role == "superadmin"


def test_verify_access_token_rejects_garbage(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret_key", "test-secret-key-at-least-32-bytes-long!!")
    with pytest.raises(InvalidTokenError):
        verify_access_token("not-a-real-token")


def test_verify_access_token_rejects_tampered_signature(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret_key", "test-secret-key-at-least-32-bytes-long!!")
    token = create_access_token(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), platform_role="user")
    with pytest.raises(InvalidTokenError):
        verify_access_token(token + "tampered")


def test_verify_access_token_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret_key", "secret-one-at-least-32-bytes-long!!!!!!")
    token = create_access_token(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), platform_role="user")
    monkeypatch.setattr(settings, "jwt_secret_key", "secret-two-at-least-32-bytes-long!!!!!!")
    with pytest.raises(InvalidTokenError):
        verify_access_token(token)


# --- app/tenancy.py: signup/login/refresh/logout, against a real (SQLite) DB --

@pytest.fixture
async def db_session_factory(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret_key", "test-secret-key-at-least-32-bytes-long!!")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_first_signup_becomes_superadmin_and_is_active(db_session_factory):
    async with db_session_factory() as session:
        result = await signup(
            session, email="first@example.com", password="a-safe-password",
            display_name="First User", workspace_name="First Co",
        )
    assert result.approval_status == "active"
    assert result.access_token and result.refresh_token


@pytest.mark.asyncio
async def test_second_signup_stays_pending_not_superadmin(db_session_factory):
    async with db_session_factory() as session:
        await signup(session, email="first@example.com", password="a-safe-password", display_name=None,
                      workspace_name=None)
    async with db_session_factory() as session:
        with pytest.raises(AuthError) as exc_info:
            await signup(session, email="second@example.com", password="a-safe-password", display_name=None,
                          workspace_name=None)
    assert exc_info.value.status_code == 202  # created but not yet approved


@pytest.mark.asyncio
async def test_signup_rejects_duplicate_email(db_session_factory):
    async with db_session_factory() as session:
        await signup(session, email="dupe@example.com", password="a-safe-password", display_name=None,
                      workspace_name=None)
    async with db_session_factory() as session:
        with pytest.raises(AuthError) as exc_info:
            await signup(session, email="dupe@example.com", password="a-different-password", display_name=None,
                          workspace_name=None)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_login_with_correct_password_succeeds(db_session_factory):
    async with db_session_factory() as session:
        await signup(session, email="user@example.com", password="a-safe-password", display_name=None,
                      workspace_name=None)
    async with db_session_factory() as session:
        result = await login(session, email="user@example.com", password="a-safe-password")
    assert result.email == "user@example.com"


@pytest.mark.asyncio
async def test_login_with_wrong_password_raises_401(db_session_factory):
    async with db_session_factory() as session:
        await signup(session, email="user@example.com", password="a-safe-password", display_name=None,
                      workspace_name=None)
    async with db_session_factory() as session:
        with pytest.raises(AuthError) as exc_info:
            await login(session, email="user@example.com", password="wrong-password")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_login_with_unknown_email_raises_same_401_as_wrong_password(db_session_factory):
    """Same status code and message either way — see AuthError's docstring
    in app/tenancy.py on never revealing which one it was."""
    async with db_session_factory() as session:
        with pytest.raises(AuthError) as exc_info:
            await login(session, email="nobody@example.com", password="anything")
    assert exc_info.value.status_code == 401
    assert exc_info.value.message == "Incorrect email or password."


@pytest.mark.asyncio
async def test_login_for_pending_account_raises_403(db_session_factory):
    async with db_session_factory() as session:
        await signup(session, email="first@example.com", password="a-safe-password", display_name=None,
                      workspace_name=None)
    async with db_session_factory() as session:
        with pytest.raises(AuthError):  # second signup stays pending
            await signup(session, email="pending@example.com", password="a-safe-password", display_name=None,
                          workspace_name=None)
    async with db_session_factory() as session:
        with pytest.raises(AuthError) as exc_info:
            await login(session, email="pending@example.com", password="a-safe-password")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_refresh_issues_a_working_access_token_for_the_same_user(db_session_factory):
    # Not asserting new_access_token != result.access_token: two tokens
    # minted for the same claims within the same second-resolution iat/exp
    # are legitimately byte-identical (same payload -> same signature) —
    # that's not staleness, just JWTs being deterministic. What matters is
    # that refresh hands back a genuinely valid token for the right user.
    async with db_session_factory() as session:
        result = await signup(session, email="user@example.com", password="a-safe-password", display_name=None,
                               workspace_name=None)
    async with db_session_factory() as session:
        new_access_token = await refresh_access_token(session, raw_refresh_token=result.refresh_token)
    claims = verify_access_token(new_access_token)
    assert claims.user_id == result.user_id
    assert claims.tenant_id == result.tenant_id


@pytest.mark.asyncio
async def test_refresh_with_unknown_token_raises_401(db_session_factory):
    async with db_session_factory() as session:
        with pytest.raises(AuthError) as exc_info:
            await refresh_access_token(session, raw_refresh_token="not-a-real-refresh-token")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_the_refresh_token(db_session_factory):
    async with db_session_factory() as session:
        result = await signup(session, email="user@example.com", password="a-safe-password", display_name=None,
                               workspace_name=None)
    async with db_session_factory() as session:
        await logout(session, raw_refresh_token=result.refresh_token)
    async with db_session_factory() as session:
        with pytest.raises(AuthError) as exc_info:
            await refresh_access_token(session, raw_refresh_token=result.refresh_token)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_logout_with_unknown_token_is_a_no_op_not_an_error(db_session_factory):
    async with db_session_factory() as session:
        await logout(session, raw_refresh_token="never-issued-token")  # must not raise
