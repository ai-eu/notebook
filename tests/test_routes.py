import asyncio

import httpx

from app.config import settings
from app.main import app

KEY = "gsk_" + "a" * 52


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_login_page_asks_for_a_groq_key():
    async def scenario():
        async with _client() as client:
            response = await client.get("/api/auth/login")
            assert response.status_code == 200
            assert "Groq API key" in response.text

    asyncio.run(scenario())


def test_recordings_require_a_session():
    async def scenario():
        async with _client() as client:
            response = await client.get("/api/recordings")
            assert response.status_code == 401

    asyncio.run(scenario())


def test_index_redirects_to_the_login_page():
    async def scenario():
        async with _client() as client:
            response = await client.get("/")
            assert response.status_code == 303
            assert response.headers["location"] == "/api/auth/login"

    asyncio.run(scenario())


def test_admin_endpoints_are_gone():
    async def scenario():
        async with _client() as client:
            assert (await client.get("/api/admin/recordings")).status_code == 404
            assert (await client.get("/api/admin/recordings/rec-1")).status_code == 404

    asyncio.run(scenario())


def test_login_creates_a_session():
    async def scenario():
        async with _client() as client:
            response = await client.post("/api/auth/login", data={"key": KEY})
            assert response.status_code == 303
            assert client.cookies.get(settings.session_cookie_name)

            recordings = await client.get("/api/recordings")
            assert recordings.status_code == 200
            assert recordings.json() == []

            index = await client.get("/")
            assert index.status_code == 200
            assert KEY not in index.text

    asyncio.run(scenario())


def test_login_rejects_a_key_with_a_wrong_format():
    async def scenario():
        async with _client() as client:
            response = await client.post("/api/auth/login", data={"key": "invitation-key"})
            assert response.status_code == 401
            assert not client.cookies.get(settings.session_cookie_name)

    asyncio.run(scenario())


def test_login_is_rate_limited_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(settings, "login_rate_limit_attempts", 2)

    async def scenario():
        async with _client() as client:
            for _ in range(2):
                assert (await client.post("/api/auth/login", data={"key": "nope"})).status_code == 401
            assert (await client.post("/api/auth/login", data={"key": "nope"})).status_code == 429

    asyncio.run(scenario())


def test_logout_drops_the_session():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            assert (await client.get("/api/recordings")).status_code == 200

            await client.get("/api/auth/logout")
            assert (await client.get("/api/recordings")).status_code == 401

    asyncio.run(scenario())
