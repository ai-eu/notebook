import asyncio

import httpx
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.main import app
from app.models import Recording, User
from app.services import processing
from app.services import transcriber

KEY = "gsk_" + "a" * 52


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_stage_helpers_store_and_clear_progress():
    processing.set_processing_stage("rec-x", "denoising")
    progress = processing.get_processing_progress("rec-x")
    assert progress["stage"] == "denoising"
    assert "started_at" in progress

    processing.set_processing_stage("rec-x", "transcribing", chunks_done=1, chunks_total=3)
    progress = processing.get_processing_progress("rec-x")
    assert progress["chunks_done"] == 1
    assert progress["chunks_total"] == 3

    processing._clear_processing_progress("rec-x")
    assert processing.get_processing_progress("rec-x") == {}


def test_transcribe_chunks_reports_chunk_progress(monkeypatch, tmp_path):
    calls = []

    async def fake_transcribe(path, api_key, language=None):
        return {"text": "hi", "language": "en", "duration": 2.0, "segments": []}

    monkeypatch.setattr(transcriber, "transcribe_file", fake_transcribe)

    async def scenario():
        await transcriber.transcribe_chunks(
            [tmp_path / "a.mp3", tmp_path / "b.mp3"],
            "key",
            progress_cb=lambda done, total: calls.append((done, total)),
        )

    asyncio.run(scenario())
    assert calls == [(1, 2), (2, 2)]


def test_status_endpoint_includes_processing_progress():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            async with AsyncSessionLocal() as db:
                user_id = (await db.execute(select(User))).scalars().first().id
                db.add(
                    Recording(
                        user_id=user_id,
                        recording_id="rec-progress",
                        folder_path=f"{user_id}/rec-progress",
                        original_filename="x.m4a",
                        status="processing",
                    )
                )
                await db.commit()

            processing.set_processing_stage("rec-progress", "denoising")
            data = (await client.get("/api/recordings/rec-progress/status")).json()
            assert data["progress"]["stage"] == "denoising"
            assert "started_at" in data["progress"]

    try:
        asyncio.run(scenario())
    finally:
        processing._clear_processing_progress("rec-progress")


def test_progress_cleared_after_processing(monkeypatch, tmp_path):
    from tests.test_processing import _create_recording, _load, _stub_media_pipeline

    async def fake_transcribe(path, api_key, language=None):
        return {"text": "hi", "segments": [], "duration": 5.0}

    _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)

    async def scenario():
        _, recording_id = await _create_recording()
        processing.set_processing_stage(recording_id, "denoising")
        await processing.process_recording(recording_id)
        recording, _ = await _load(recording_id)
        assert recording.status == "done"
        assert processing.get_processing_progress(recording_id) == {}

    asyncio.run(scenario())
