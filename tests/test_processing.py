import asyncio

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Recording, StoredFile, User
from app.services import processing
from app.services.groq import GroqAuthError
from app.services.storage import archive

KEY = "gsk_" + "a" * 52


def _stub_media_pipeline(monkeypatch, tmp_path, transcribe):
    """Point the processing pipeline at a fake recording folder and stub out ffmpeg."""
    folder = tmp_path / "recording"
    folder.mkdir()
    (folder / "original.tmp").write_bytes(b"original")

    async def fake_info(path):
        return {}

    async def fake_convert(src, dst, passthrough=False):
        dst.write_bytes(b"mp3")

    async def fake_duration(path):
        return 5.0

    monkeypatch.setattr(processing, "resolve_recording_path", lambda path: folder)
    monkeypatch.setattr(processing, "get_media_info", fake_info)
    monkeypatch.setattr(processing, "has_audio_stream", lambda info: True)
    monkeypatch.setattr(processing, "is_mp3_passthrough", lambda info: False)
    monkeypatch.setattr(processing, "convert_to_mp3", fake_convert)
    monkeypatch.setattr(processing, "get_duration", fake_duration)
    monkeypatch.setattr(processing, "get_file_size_mb", lambda path: 1.0)
    monkeypatch.setattr(processing, "transcribe_file", transcribe)
    monkeypatch.setattr(processing, "transcribe_chunks", transcribe)
    monkeypatch.setattr(settings, "mock_transcription", False)
    return folder


async def _create_recording() -> tuple[int, str]:
    async with AsyncSessionLocal() as db:
        user = User(groq_key=KEY, label="Alice")
        db.add(user)
        await db.commit()
        recording = Recording(
            user_id=user.id,
            recording_id="rec-1",
            folder_path="1/rec-1",
            original_filename="note.m4a",
            status="pending",
        )
        db.add(recording)
        await db.commit()
        return user.id, recording.recording_id


async def _load(recording_id: str) -> tuple[Recording, User]:
    async with AsyncSessionLocal() as db:
        recording = (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one()
        user = await db.get(User, recording.user_id)
        return recording, user


def test_transcription_uses_the_key_of_the_uploader(monkeypatch, tmp_path):
    captured = {}

    async def fake_transcribe(path, api_key, language=None):
        captured["api_key"] = api_key
        return {"text": "hi", "segments": [], "duration": 5.0}

    folder = _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, user = await _load(recording_id)
        assert captured["api_key"] == KEY
        assert recording.status == "done"
        assert recording.duration == 5.0
        assert user.key_valid is True
        assert (folder / "formatted.txt").read_text(encoding="utf-8") == "hi"

    asyncio.run(scenario())


def test_rejected_key_marks_the_recording_and_the_user(monkeypatch, tmp_path):
    async def fake_transcribe(path, api_key, language=None):
        raise GroqAuthError("Groq rejected the API key (HTTP 401)")

    _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, user = await _load(recording_id)
        assert recording.status == "error"
        assert recording.error_message == "Groq rejected the API key. Sign in again with a valid key."
        assert user.key_valid is False

    asyncio.run(scenario())


def test_other_failures_keep_the_key_marked_as_valid(monkeypatch, tmp_path):
    async def fake_transcribe(path, api_key, language=None):
        raise ValueError("Groq API error 500: boom")

    _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, user = await _load(recording_id)
        assert recording.status == "error"
        assert "500" in recording.error_message
        assert user.key_valid is True

    asyncio.run(scenario())


def _stub_archive(monkeypatch, folder):
    monkeypatch.setattr(archive, "resolve_recording_path", lambda path: folder)


def test_a_finished_recording_is_copied_into_the_channel(monkeypatch, tmp_path, bot_api):
    async def fake_transcribe(path, api_key, language=None):
        return {"text": "hi", "segments": [], "duration": 5.0}

    folder = _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)
    _stub_archive(monkeypatch, folder)

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, _ = await _load(recording_id)
        assert recording.status == "done"
        assert recording.storage_state == "tg"
        assert recording.archived_at is not None
        assert bot_api.calls.count("sendDocument") == 3
        async with AsyncSessionLocal() as db:
            files = (
                await db.execute(select(StoredFile).where(StoredFile.recording_id == recording_id))
            ).scalars().all()
        assert {(row.kind, row.idx) for row in files} == {
            ("audio", 0),
            ("transcript", 0),
            ("formatted", 0),
        }

    asyncio.run(scenario())


def test_a_failed_transcription_is_not_archived(monkeypatch, tmp_path, bot_api):
    async def fake_transcribe(path, api_key, language=None):
        raise ValueError("Groq API error 500: boom")

    folder = _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)
    _stub_archive(monkeypatch, folder)

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, _ = await _load(recording_id)
        assert recording.status == "error"
        assert recording.storage_state == "local"
        assert bot_api.calls == []

    asyncio.run(scenario())


def test_a_broken_archive_does_not_fail_the_transcription(monkeypatch, tmp_path, bot_api):
    async def fake_transcribe(path, api_key, language=None):
        return {"text": "hi", "segments": [], "duration": 5.0}

    folder = _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)
    _stub_archive(monkeypatch, folder)
    bot_api.failures["sendDocument"] = [(500, {"ok": False, "description": "Internal Server Error"})] * 3

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, _ = await _load(recording_id)
        # the transcript is what the user asked for; the channel can catch up later
        assert recording.status == "done"
        assert recording.storage_state == "local"
        assert (folder / "transcript.json").exists()

    asyncio.run(scenario())
