import asyncio

from sqlalchemy import select
from starlette.requests import Request

from app.auth import (
    clear_login_failures,
    client_ip,
    login_rate_limited,
    login_with_groq_key,
    record_login_failure,
)
from app.config import settings
from app.database import AsyncSessionLocal
from app.models import User
from app.services import groq
from conftest import FakeAsyncClient, FakeResponse

KEY = "gsk_" + "a" * 52


async def _add_existing_user(db, key: str = KEY, label: str = "Alice") -> User:
    user = User(groq_key=key, label=label)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def test_registers_a_new_user():
    async def scenario():
        async with AsyncSessionLocal() as db:
            user, error = await login_with_groq_key(db, KEY)
            assert error is None
            assert user.groq_key == KEY
            assert user.label == groq.mask_key(KEY)
            assert user.key_valid is True
            assert user.last_verified_at is not None

    asyncio.run(scenario())


def test_second_login_reuses_the_same_user():
    async def scenario():
        async with AsyncSessionLocal() as db:
            first, _ = await login_with_groq_key(db, KEY)
            second, _ = await login_with_groq_key(db, f"  {KEY}  ")
            assert first.id == second.id
            assert len((await db.execute(select(User))).scalars().all()) == 1

    asyncio.run(scenario())


def test_rejects_a_key_with_a_wrong_format():
    async def scenario():
        async with AsyncSessionLocal() as db:
            user, error = await login_with_groq_key(db, "invitation-key")
            assert user is None
            assert "gsk_" in error
            assert (await db.execute(select(User))).scalars().all() == []

    asyncio.run(scenario())


def test_rejects_unknown_keys_when_self_registration_is_off(monkeypatch):
    monkeypatch.setattr(settings, "allow_self_registration", False)

    async def scenario():
        async with AsyncSessionLocal() as db:
            user, error = await login_with_groq_key(db, KEY)
            assert user is None
            assert "not allowed" in error

    asyncio.run(scenario())


def test_known_user_can_still_sign_in_when_self_registration_is_off(monkeypatch):
    monkeypatch.setattr(settings, "allow_self_registration", False)

    async def scenario():
        async with AsyncSessionLocal() as db:
            await _add_existing_user(db)
            user, error = await login_with_groq_key(db, KEY)
            assert error is None
            assert user.label == "Alice"

    asyncio.run(scenario())


def test_new_key_is_rejected_when_groq_says_it_is_invalid(monkeypatch):
    monkeypatch.setattr(settings, "mock_transcription", False)

    async def fake_validate(key):
        return "invalid"

    monkeypatch.setattr("app.auth.validate_key", fake_validate)

    async def scenario():
        async with AsyncSessionLocal() as db:
            user, error = await login_with_groq_key(db, KEY)
            assert user is None
            assert "rejected" in error

    asyncio.run(scenario())


def test_new_key_is_rejected_when_groq_is_unreachable(monkeypatch):
    monkeypatch.setattr(settings, "mock_transcription", False)

    async def fake_validate(key):
        return "unreachable"

    monkeypatch.setattr("app.auth.validate_key", fake_validate)

    async def scenario():
        async with AsyncSessionLocal() as db:
            user, error = await login_with_groq_key(db, KEY)
            assert user is None
            assert "Could not verify" in error

    asyncio.run(scenario())


def test_throttled_key_registers_the_user(monkeypatch):
    monkeypatch.setattr(settings, "mock_transcription", False)

    async def fake_validate(key):
        return "throttled"

    monkeypatch.setattr("app.auth.validate_key", fake_validate)

    async def scenario():
        async with AsyncSessionLocal() as db:
            user, error = await login_with_groq_key(db, KEY)
            assert error is None
            assert user.key_valid is True

    asyncio.run(scenario())


def test_revoked_key_does_not_lock_the_user_out_of_their_data(monkeypatch):
    monkeypatch.setattr(settings, "mock_transcription", False)

    async def fake_validate(key):
        return "invalid"

    monkeypatch.setattr("app.auth.validate_key", fake_validate)

    async def scenario():
        async with AsyncSessionLocal() as db:
            await _add_existing_user(db)
            user, error = await login_with_groq_key(db, KEY)
            assert error is None
            assert user.key_valid is False

    asyncio.run(scenario())


def test_groq_outage_does_not_lock_a_known_user_out(monkeypatch):
    monkeypatch.setattr(settings, "mock_transcription", False)

    async def fake_validate(key):
        return "unreachable"

    monkeypatch.setattr("app.auth.validate_key", fake_validate)

    async def scenario():
        async with AsyncSessionLocal() as db:
            await _add_existing_user(db)
            user, error = await login_with_groq_key(db, KEY)
            assert error is None
            assert user.key_valid is True

    asyncio.run(scenario())


def test_validate_key_maps_groq_responses(monkeypatch):
    async def check(status_code: int, expected: str):
        fake = FakeAsyncClient(FakeResponse(status_code, {"error": "x"}))
        monkeypatch.setattr(groq.httpx, "AsyncClient", lambda **kwargs: fake)
        assert await groq.validate_key(KEY) == expected
        assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"

    asyncio.run(check(200, "valid"))
    asyncio.run(check(401, "invalid"))
    asyncio.run(check(403, "invalid"))
    asyncio.run(check(429, "throttled"))
    asyncio.run(check(500, "unreachable"))


def test_validate_key_reports_a_network_failure(monkeypatch):
    class BrokenClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, headers=None, **kwargs):
            raise groq.httpx.ConnectError("no route to host")

    monkeypatch.setattr(groq.httpx, "AsyncClient", BrokenClient)
    assert asyncio.run(groq.validate_key(KEY)) == "unreachable"


def test_rate_limit_counts_failures_per_ip(monkeypatch):
    monkeypatch.setattr(settings, "login_rate_limit_attempts", 3)
    monkeypatch.setattr(settings, "login_rate_limit_window_seconds", 60)
    clear_login_failures("1.2.3.4")

    for _ in range(3):
        assert not login_rate_limited("1.2.3.4")
        record_login_failure("1.2.3.4")

    assert login_rate_limited("1.2.3.4")
    assert not login_rate_limited("5.6.7.8")

    clear_login_failures("1.2.3.4")
    assert not login_rate_limited("1.2.3.4")


def test_rate_limit_ignores_a_missing_ip():
    assert not login_rate_limited(None)
    record_login_failure(None)
    clear_login_failures(None)


def _request(headers: dict | None = None, client: tuple | None = ("10.0.0.1", 1234)) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": client,
        }
    )


def test_proxy_headers_are_ignored_by_default(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    request = _request({"x-forwarded-for": "1.2.3.4", "x-real-ip": "5.6.7.8"})
    assert client_ip(request) == "10.0.0.1"


def test_proxy_headers_are_used_when_trusted(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    assert client_ip(_request({"x-forwarded-for": "1.2.3.4, 10.0.0.1"})) == "1.2.3.4"
    assert client_ip(_request({"x-real-ip": "5.6.7.8"})) == "5.6.7.8"
    assert client_ip(_request()) == "10.0.0.1"


def test_client_ip_without_a_peer(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    assert client_ip(_request(client=None)) is None
