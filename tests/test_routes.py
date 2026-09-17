import asyncio

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.main import app
from app.models import Recording, User
from app.services.storage import archive

KEY = "gsk_" + "a" * 52


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _signed_in_user_id() -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User))).scalars().first().id


async def _create_recording(user_id: int, recording_id: str = "rec-1", audio: bytes = b"lecture" * 100):
    """A finished recording that lives where the app expects it (inside DATA_DIR)."""
    folder = settings.data_dir_absolute / str(user_id) / recording_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "audio.mp3").write_bytes(audio)
    (folder / "transcript.json").write_text('{"text": "hi", "segments": []}', encoding="utf-8")
    async with AsyncSessionLocal() as db:
        db.add(
            Recording(
                user_id=user_id,
                recording_id=recording_id,
                folder_path=f"{user_id}/{recording_id}",
                original_filename="lecture.m4a",
                status="done",
            )
        )
        await db.commit()
    return folder


def test_txt_download_appears_only_when_the_file_is_ready():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            folder = await _create_recording(await _signed_in_user_id())

            pending_page = await client.get("/t/rec-1")
            assert 'id="download-link"' in pending_page.text
            assert 'download-link hidden' in pending_page.text
            assert (await client.get("/api/recordings/rec-1/status")).json()["txt_ready"] is False

            (folder / "formatted.txt").write_text("hi", encoding="utf-8")

            ready_page = await client.get("/t/rec-1")
            assert 'download-link hidden' not in ready_page.text
            assert (await client.get("/api/recordings/rec-1/status")).json()["txt_ready"] is True

    asyncio.run(scenario())


def test_login_page_asks_for_a_groq_key():
    async def scenario():
        async with _client() as client:
            response = await client.get("/api/auth/login")
            assert response.status_code == 200
            assert "Groq API key" in response.text

    asyncio.run(scenario())


def test_pages_bootstrap_the_color_theme():
    async def scenario():
        async with _client() as client:
            response = await client.get("/api/auth/login")
            assert response.status_code == 200
            assert 'name="theme-color"' in response.text
            assert "window.applyTheme" in response.text

    asyncio.run(scenario())


def test_index_offers_the_theme_toggle():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            response = await client.get("/")
            assert response.status_code == 200
            assert 'id="theme-toggle"' in response.text

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


def test_audio_is_served_locally_while_it_is_still_here(bot_api):
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            await _create_recording(await _signed_in_user_id())

            response = await client.get("/api/recordings/rec-1/audio")

            assert response.status_code == 200
            assert response.content == b"lecture" * 100
            assert bot_api.calls == []

    asyncio.run(scenario())


def test_audio_is_pulled_back_from_the_archive_when_it_is_gone(bot_api):
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            folder = await _create_recording(await _signed_in_user_id())
            await archive.archive_recording("rec-1")
            (folder / "audio.mp3").unlink()

            response = await client.get("/api/recordings/rec-1/audio")

            assert response.status_code == 200
            assert response.content == b"lecture" * 100
            assert "download" in bot_api.calls
            # the download is cached, so a reload does not hit Telegram again
            bot_api.calls.clear()
            assert (await client.get("/api/recordings/rec-1/audio")).status_code == 200
            assert bot_api.calls == []

    asyncio.run(scenario())


def test_audio_answers_503_when_the_archive_cannot_be_reached(bot_api):
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            folder = await _create_recording(await _signed_in_user_id())
            await archive.archive_recording("rec-1")
            (folder / "audio.mp3").unlink()
            bot_api.failures["getFile"] = [(400, {"ok": False, "description": "Bad Request: file is not found"})] * 2
            bot_api.failures["copyMessage"] = [(400, {"ok": False, "description": "Bad Request: not found"})]

            response = await client.get("/api/recordings/rec-1/audio")

            assert response.status_code == 503
            assert "archive" in response.json()["detail"]

    asyncio.run(scenario())


def test_deleting_a_recording_also_deletes_the_channel_copy(bot_api):
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            folder = await _create_recording(await _signed_in_user_id())
            await archive.archive_recording("rec-1")

            response = await client.delete("/api/recordings/rec-1")

            assert response.status_code == 200
            assert bot_api.messages == {}
            assert not folder.exists()
            assert (await client.get("/api/recordings")).json() == []

    asyncio.run(scenario())


def test_transcript_page_offers_playback_speed_control():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            await _create_recording(await _signed_in_user_id())

            response = await client.get("/t/rec-1")

            assert response.status_code == 200
            assert 'id="speed-btn"' in response.text
            assert 'id="speed-slider"' in response.text

    asyncio.run(scenario())


def test_recording_tags_and_comment_can_be_updated():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            await _create_recording(await _signed_in_user_id())

            response = await client.put(
                "/api/recordings/rec-1/tags",
                json={"tags": "  lecture   bom   lecture ", "comment": "  first lecture  "},
            )

            assert response.status_code == 200
            assert response.json()["tags"] == "lecture bom"
            assert response.json()["comment"] == "first lecture"

            listing = (await client.get("/api/recordings")).json()
            assert listing[0]["tags"] == "lecture bom"
            assert listing[0]["comment"] == "first lecture"

            cleared = await client.put(
                "/api/recordings/rec-1/tags",
                json={"tags": "", "comment": ""},
            )
            assert cleared.status_code == 200
            assert cleared.json()["tags"] == ""
            assert cleared.json()["comment"] is None

    asyncio.run(scenario())


def test_tags_require_a_session():
    async def scenario():
        async with _client() as client:
            response = await client.put("/api/recordings/rec-1/tags", json={"tags": "x", "comment": ""})
            assert response.status_code == 401

    asyncio.run(scenario())


def test_index_offers_the_tag_filter_bar():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            response = await client.get("/")
            assert response.status_code == 200
            assert 'id="tags-filter"' in response.text

    asyncio.run(scenario())
