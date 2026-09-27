"""One-time device link login: add a device without entering the Groq key on it."""

import asyncio

import httpx
from app.config import settings
from app.main import app

KEY = "gsk_" + "b" * 52


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_device_link_redeems_once_and_expires():
    async def scenario():
        async with _client() as client:
            login = await client.post("/api/auth/login", data={"key": KEY})
            assert login.status_code == 303

            response = await client.get("/api/auth/device/link")
            assert response.status_code == 200
            body = response.json()
            url = body["url"]
            assert "/api/auth/device/redeem/" in url

            # The already-authenticated client can also redeem; more importantly,
            # a fresh client (no cookies) must get a session from the link.
            async with _client() as fresh:
                redeem = await fresh.get(url, follow_redirects=False)
                assert redeem.status_code == 303
                assert settings.session_cookie_name in redeem.cookies

                # Second use of the same link must fail: it is one-time.
                again = await fresh.get(url, follow_redirects=False)
                assert again.status_code == 410

    asyncio.run(scenario())


def test_device_link_requires_authentication():
    async def scenario():
        async with _client() as client:
            response = await client.get("/api/auth/device/link", follow_redirects=False)
            assert response.status_code in (303, 401, 403)

    asyncio.run(scenario())


def test_unknown_token_is_rejected():
    async def scenario():
        async with _client() as client:
            response = await client.get(
                "/api/auth/device/redeem/not-a-real-token", follow_redirects=False
            )
            assert response.status_code == 410

    asyncio.run(scenario())
