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

    folder = _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, user = await _load(recording_id)
        assert recording.status == "error"
        assert "500" in recording.error_message
        assert user.key_valid is True
        # The original is kept so the user can retry without uploading again.
        assert (folder / "original.tmp").exists()

    asyncio.run(scenario())


def test_chunks_are_removed_after_processing(monkeypatch, tmp_path):
    async def fake_transcribe(path, api_key, language=None):
        return {"text": "hi", "segments": [{"start": 0, "end": 1, "text": "hi"}], "duration": 5.0}

    folder = _stub_media_pipeline(monkeypatch, tmp_path, fake_transcribe)

    async def fake_split(path, output_dir, minutes, passthrough=False):
        output_dir.mkdir(parents=True, exist_ok=True)
        chunks = []
        for index in range(2):
            chunk = output_dir / f"chunk_{index:03d}.mp3"
            chunk.write_bytes(b"chunk")
            chunks.append(chunk)
        return chunks

    async def fake_chunks(paths, api_key, progress_cb=None):
        if progress_cb:
            progress_cb(len(paths), len(paths))
        return {"text": "hi", "segments": [{"start": 0, "end": 1, "text": "hi"}], "duration": 5.0}

    monkeypatch.setattr(processing, "get_file_size_mb", lambda path: 100.0)
    monkeypatch.setattr(processing, "split_audio", fake_split)
    monkeypatch.setattr(processing, "transcribe_chunks", fake_chunks)

    async def scenario():
        _, recording_id = await _create_recording()
        await processing.process_recording(recording_id)
        recording, _ = await _load(recording_id)
        assert recording.status == "done"
        assert not (folder / "chunks").exists()
        assert not (folder / "original.tmp").exists()

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
        assert bot_api.calls.count("sendDocument") == 4
        async with AsyncSessionLocal() as db:
            files = (
                await db.execute(select(StoredFile).where(StoredFile.recording_id == recording_id))
            ).scalars().all()
        assert {(row.kind, row.idx) for row in files} == {
            ("audio", 0),
            ("transcript", 0),
            ("sentences", 0),
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


def test_recovery_requeues_a_processing_recording(monkeypatch, tmp_path):
    folder = tmp_path / "recording"
    folder.mkdir()
    (folder / "original.tmp").write_bytes(b"original")
    monkeypatch.setattr(processing, "resolve_recording_path", lambda path: folder)

    submitted = []
    calls = []

    class FakeRunner:
        def submit(self, factory):
            submitted.append(factory)

    async def fake_process(recording_id, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(processing, "job_runner", FakeRunner())
    monkeypatch.setattr(processing, "process_recording", fake_process)

    async def scenario():
        async with AsyncSessionLocal() as db:
            user = User(groq_key=KEY, label="Alice")
            db.add(user)
            await db.commit()
            db.add(
                Recording(
                    user_id=user.id,
                    recording_id="rec-recover",
                    folder_path="1/rec-recover",
                    original_filename="note.m4a",
                    status="processing",
                )
            )
            await db.commit()

        summary = await processing.recover_interrupted_jobs()
        assert summary["requeued"] == 1
        assert len(submitted) == 1

        async with AsyncSessionLocal() as db:
            recording = (
                await db.execute(select(Recording).where(Recording.recording_id == "rec-recover"))
            ).scalar_one()
            assert recording.status == "pending"

        for factory in submitted:
            await factory()
        assert calls == [{}]

    asyncio.run(scenario())


def test_recovery_resumes_from_audio_after_a_crash(monkeypatch, tmp_path):
    folder = tmp_path / "recording"
    folder.mkdir()
    (folder / "audio.mp3").write_bytes(b"mp3")
    monkeypatch.setattr(processing, "resolve_recording_path", lambda path: folder)

    submitted = []
    calls = []

    class FakeRunner:
        def submit(self, factory):
            submitted.append(factory)

    async def fake_process(recording_id, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(processing, "job_runner", FakeRunner())
    monkeypatch.setattr(processing, "process_recording", fake_process)

    async def scenario():
        async with AsyncSessionLocal() as db:
            user = User(groq_key=KEY + "b", label="Bob")
            db.add(user)
            await db.commit()
            db.add(
                Recording(
                    user_id=user.id,
                    recording_id="rec-resume",
                    folder_path="1/rec-resume",
                    original_filename="note.m4a",
                    status="processing",
                )
            )
            await db.commit()

        summary = await processing.recover_interrupted_jobs()
        assert summary["requeued"] == 1
        for factory in submitted:
            await factory()
        assert calls == [{"resume_audio": True}]

    asyncio.run(scenario())


def test_recovery_gives_up_after_too_many_attempts(monkeypatch, tmp_path):
    folder = tmp_path / "recording"
    folder.mkdir()
    (folder / "original.tmp").write_bytes(b"original")
    monkeypatch.setattr(processing, "resolve_recording_path", lambda path: folder)
    monkeypatch.setattr(settings, "max_job_attempts", 3)

    submitted = []

    class FakeRunner:
        def submit(self, factory):
            submitted.append(factory)

    monkeypatch.setattr(processing, "job_runner", FakeRunner())

    async def scenario():
        async with AsyncSessionLocal() as db:
            user = User(groq_key=KEY + "c", label="Carol")
            db.add(user)
            await db.commit()
            db.add(
                Recording(
                    user_id=user.id,
                    recording_id="rec-failed",
                    folder_path="1/rec-failed",
                    original_filename="note.m4a",
                    status="processing",
                    attempts=3,
                )
            )
            await db.commit()

        summary = await processing.recover_interrupted_jobs()
        assert summary["failed"] == 1
        assert submitted == []

        async with AsyncSessionLocal() as db:
            recording = (
                await db.execute(select(Recording).where(Recording.recording_id == "rec-failed"))
            ).scalar_one()
            assert recording.status == "error"

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
