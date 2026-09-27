"""Device login code: add a device without entering the Groq key on it.

Covers: code login on a fresh device, one-time use, revocation of the active
code, and per-session revocation (access management).
"""

import asyncio

import httpx
from app.config import settings
from app.main import app

KEY = "gsk_" + "b" * 52


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_device_code_logs_in_a_fresh_device_once():
    async def scenario():
        async with _client() as client:
            login = await client.post("/api/auth/login", data={"key": KEY})
            assert login.status_code == 303

            response = await client.post("/api/auth/device/code")
            assert response.status_code == 200
            body = response.json()
            code = body["code"]
            assert len(code) == 6 and code.isdigit()
            assert body["ttl_seconds"] > 0

            # A fresh device (no cookies) logs in with just the code.
            async with _client() as fresh:
                redeem = await fresh.post(
                    "/api/auth/login", data={"code": code}, follow_redirects=False
                )
                assert redeem.status_code == 303
                assert settings.session_cookie_name in redeem.cookies

                # Second use of the same code must fail: it is one-time.
                again = await fresh.post("/api/auth/login", data={"code": code})
                assert again.status_code == 401

    asyncio.run(scenario())


def test_new_code_and_revoke_invalidate_the_previous_code():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})

            first = (await client.post("/api/auth/device/code")).json()["code"]
            # Generating a new code invalidates the previous one.
            second = (await client.post("/api/auth/device/code")).json()["code"]
            assert first != second

            async with _client() as fresh:
                used_first = await fresh.post("/api/auth/login", data={"code": first})
                assert used_first.status_code == 401

            # Explicit revocation invalidates the active code too.
            revoked = await client.post("/api/auth/device/revoke-code")
            assert revoked.status_code == 200
            async with _client() as fresh:
                used_second = await fresh.post("/api/auth/login", data={"code": second})
                assert used_second.status_code == 401

    asyncio.run(scenario())


def test_device_code_requires_authentication():
    async def scenario():
        async with _client() as client:
            response = await client.post("/api/auth/device/code", follow_redirects=False)
            assert response.status_code in (303, 401, 403)

    asyncio.run(scenario())


def test_sessions_list_and_revocation():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            # A second "device" joins via code.
            code = (await client.post("/api/auth/device/code")).json()["code"]
            async with _client() as other:
                await other.post("/api/auth/login", data={"code": code})

                listing = await client.get("/api/auth/sessions")
                sessions = listing.json()["sessions"]
                assert len(sessions) == 2
                assert any(s["current"] for s in sessions)
                other_session = next(s for s in sessions if not s["current"])

                deleted = await client.delete(
                    "/api/auth/sessions/" + other_session["id"]
                )
                assert deleted.status_code == 200

                # The revoked device is logged out.
                after = await other.get("/api/recordings")
                assert after.status_code in (303, 401)

                # Deleting an unknown/other session is a clean no.
                missing = await client.delete("/api/auth/sessions/nope")
                assert missing.json()["ok"] is False

    asyncio.run(scenario())
