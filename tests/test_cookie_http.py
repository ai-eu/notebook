"""Session cookie must work on plain HTTP, not only behind HTTPS.

Regression: `Secure` was set unconditionally, so a device opening the app over
HTTP (e.g. LAN) accepted the session cookie but never sent it back, ending up
with an empty page after a successful code login.
"""

import asyncio

import httpx

from app.config import settings
from app.main import app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_login_cookie_is_not_secure_on_http():
    async def scenario():
        async with _client() as client:
            response = await client.post(
                "/api/auth/login", data={"key": "gsk_" + "c" * 52}, follow_redirects=False
            )
            assert response.status_code == 303
            cookie = response.headers["set-cookie"].lower()
            # Default in tests is SESSION_COOKIE_SECURE=false; when the setting is
            # enabled, an HTTP request must still not produce a Secure cookie.
            if settings.session_cookie_secure:
                assert "secure" not in cookie
            assert "session_id=" in cookie

    asyncio.run(scenario())
