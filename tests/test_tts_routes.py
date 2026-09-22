import asyncio

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.main import app
from app.models import Recording
from app.services import processing

KEY = "gsk_" + "a" * 52


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _stub_tts_pipeline(monkeypatch):
    async def fake_process_tts(recording_id, voice=None):
        async with AsyncSessionLocal() as db:
            recording = (
                await db.execute(
                    select(Recording).where(Recording.recording_id == recording_id)
                )
            ).scalar_one()
            recording.status = "done"
            recording.tts_model = "edge"
            recording.tts_voice = (voice or "edge:en-US-GuyNeural").split(":", 1)[-1]
            await db.commit()

    monkeypatch.setattr(processing, "process_tts", fake_process_tts)
    import app.routers.tts as tts_router

    monkeypatch.setattr(tts_router, "process_tts", fake_process_tts)


def test_voices_endpoint(monkeypatch):
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            response = await client.get("/api/tts/voices")
            assert response.status_code == 200
            data = response.json()
            assert data["provider"] == "edge"
            assert data["enabled"] is True
            langs = {voice["lang"] for voice in data["voices"]}
            assert langs == {"en", "pt-PT", "es", "uk"}
            defaults = [voice for voice in data["voices"] if voice.get("default")]
            assert len(defaults) == 1
            assert defaults[0]["id"] == "edge:en-US-GuyNeural"

    asyncio.run(scenario())


def test_tts_upload_happy_path(monkeypatch):
    _stub_tts_pipeline(monkeypatch)

    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            response = await client.post(
                "/api/tts/upload",
                files={"file": ("note.md", "Hello TTS world.".encode("utf-8"), "text/markdown")},
                data={"voice": "edge:pt-PT-DuarteNeural"},
            )
            assert response.status_code == 200
            data = response.json()
            recording_id = data["recording_id"]

            async with AsyncSessionLocal() as db:
                recording = (
                    await db.execute(
                        select(Recording).where(Recording.recording_id == recording_id)
                    )
                ).scalar_one()
            assert recording.kind == "tts"
            assert recording.original_filename == "note.md"
            assert recording.status == "done"
            assert recording.tts_voice == "pt-PT-DuarteNeural"
            folder = settings.data_dir_absolute / str(recording.user_id) / recording_id
            assert (folder / "original.tmp").read_text(encoding="utf-8") == "Hello TTS world."

    asyncio.run(scenario())


def test_tts_upload_rejects_wrong_extension():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            response = await client.post(
                "/api/tts/upload",
                files={"file": ("song.mp3", b"id3", "audio/mpeg")},
            )
            assert response.status_code == 400
            assert "txt" in response.json()["detail"]

    asyncio.run(scenario())


def test_tts_upload_rejects_too_long_text(monkeypatch):
    monkeypatch.setattr(settings, "tts_max_chars", 10)

    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            response = await client.post(
                "/api/tts/upload",
                files={"file": ("note.txt", b"x" * 11, "text/plain")},
            )
            assert response.status_code == 400
            assert "too long" in response.json()["detail"]

    asyncio.run(scenario())


def test_tts_upload_rejects_empty_text(monkeypatch):
    _stub_tts_pipeline(monkeypatch)

    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            response = await client.post(
                "/api/tts/upload",
                files={"file": ("note.txt", b"   \n ", "text/plain")},
            )
            assert response.status_code == 400
            assert "Empty" in response.json()["detail"]

    asyncio.run(scenario())


def test_tts_upload_requires_a_session():
    async def scenario():
        async with _client() as client:
            response = await client.post(
                "/api/tts/upload",
                files={"file": ("note.txt", b"hi", "text/plain")},
            )
            assert response.status_code == 401
            assert (await client.get("/api/tts/voices")).status_code == 401

    asyncio.run(scenario())


def test_tts_upload_disabled(monkeypatch):
    monkeypatch.setattr(settings, "tts_enabled", False)

    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            response = await client.post(
                "/api/tts/upload",
                files={"file": ("note.txt", b"hi", "text/plain")},
            )
            assert response.status_code == 404

    asyncio.run(scenario())


def test_status_reports_tts_fields(monkeypatch):
    _stub_tts_pipeline(monkeypatch)

    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            await client.post(
                "/api/tts/upload",
                files={"file": ("note.txt", "hi".encode("utf-8"), "text/plain")},
            )
            async with AsyncSessionLocal() as db:
                recording = (await db.execute(select(Recording))).scalars().first()
                recording.status = "processing"
                await db.commit()
                recording_id = recording.recording_id

            processing.set_tts_progress(recording_id, 2, 5)
            data = (await client.get(f"/api/recordings/{recording_id}/status")).json()
            assert data["kind"] == "tts"
            assert data["status"] == "processing"
            assert data["tts_progress"] == {"chunks_done": 2, "chunks_total": 5}

            async with AsyncSessionLocal() as db:
                recording = (
                    await db.execute(
                        select(Recording).where(Recording.recording_id == recording_id)
                    )
                ).scalar_one()
                recording.status = "done"
                recording.tts_model = "edge"
                recording.tts_voice = "en-US-GuyNeural"
                await db.commit()
            processing.tts_progress.pop(recording_id, None)

            data = (await client.get(f"/api/recordings/{recording_id}/status")).json()
            assert data["tts_model"] == "edge"
            assert data["tts_voice"] == "en-US-GuyNeural"
            assert data["tts_progress"] == {}

    asyncio.run(scenario())


def test_index_shows_tts_tabs_when_enabled():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            page = await client.get("/")
            assert 'id="mode-tabs"' in page.text
            assert 'id="tts-dropzone"' in page.text
            assert 'id="voice-select"' in page.text
            assert "Text to Speech" in page.text

    asyncio.run(scenario())


def test_index_hides_tts_tabs_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "tts_enabled", False)

    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            page = await client.get("/")
            assert 'id="mode-tabs"' not in page.text
            assert 'id="tts-dropzone"' not in page.text

    asyncio.run(scenario())


def test_status_of_a_transcription_has_no_tts_fields():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            response = await client.post(
                "/api/upload",
                files={"file": ("rec.m4a", b"audio-bytes", "audio/mp4")},
            )
            assert response.status_code == 200
            recording_id = response.json()["recording_id"]
            data = (await client.get(f"/api/recordings/{recording_id}/status")).json()
            assert "kind" not in data
            assert "tts_progress" not in data

    asyncio.run(scenario())
