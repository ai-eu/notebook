import asyncio
import hashlib

import pytest

from app.config import settings
from app.services.storage import archive, telegram
from conftest import load_files, load_recording, make_recording


def test_caption_round_trip():
    caption = archive.build_caption("rec-1", "audio", 1, 3, 12345, "ab" * 32)
    parsed = archive.parse_caption(caption)
    assert parsed == {"rid": "rec-1", "kind": "audio", "part": "2/3", "size": "12345", "sha": "ab" * 32}


def test_caption_label_cannot_shadow_metadata():
    caption = archive.build_caption("rec-1", "audio", 0, 1, 1, "ab" * 32, label="🎙 rid=fake;sha=bad — audio")
    parsed = archive.parse_caption(caption)
    assert parsed["rid"] == "rec-1"
    assert parsed["sha"] == "ab" * 32


def test_small_audio_is_uploaded_as_a_single_part(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"x" * 1024)

    async def no_ffprobe(path):
        raise AssertionError("small files must not be probed")

    monkeypatch.setattr(archive, "get_media_info", no_ffprobe)
    parts = asyncio.run(archive.plan_parts(folder, folder / "audio.mp3"))
    assert parts == [folder / "audio.mp3"]


def test_a_stream_that_is_not_mp3_is_split_by_ffmpeg(monkeypatch, tmp_path, bot_api):
    """The byte splitter only understands MPEG Layer III, everything else goes to ffmpeg."""
    limit = settings.telegram_chunk_mb * 1024 * 1024
    folder = make_recording(monkeypatch, tmp_path, audio=b"x" * (limit + 1))
    captured = {}

    async def fake_info(path):
        return {}

    async def fake_split(path, output_dir, minutes, passthrough=False, strip_headers=False):
        captured["minutes"] = minutes
        captured["passthrough"] = passthrough
        captured["strip_headers"] = strip_headers
        output_dir.mkdir(parents=True, exist_ok=True)
        chunks = []
        for idx, size in enumerate((limit, 1024)):
            chunk = output_dir / f"chunk_{idx:03d}.mp3"
            chunk.write_bytes(b"y" * size)
            chunks.append(chunk)
        return chunks

    monkeypatch.setattr(archive, "get_media_info", fake_info)
    monkeypatch.setattr(archive, "get_audio_bitrate_kbps", lambda info: 48)
    monkeypatch.setattr(archive, "split_audio", fake_split)

    parts = asyncio.run(archive.plan_parts(folder, folder / "audio.mp3"))
    assert len(parts) == 2
    assert all(part.stat().st_size <= limit for part in parts)
    # 48 kbps for 19 MiB is 55 minutes, capped by CHUNK_MAX_MINUTES
    assert captured["minutes"] == settings.chunk_max_minutes
    # a stream copy, cut without headers so the parts still glue together
    assert captured["passthrough"] is True
    assert captured["strip_headers"] is True


def test_archive_uploads_audio_and_transcript(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    audio_sha = hashlib.sha256((folder / "audio.mp3").read_bytes()).hexdigest()

    result = asyncio.run(archive.archive_recording("rec-1"))
    assert result == {"recording_id": "rec-1", "parts": 1, "uploaded": 2}
    assert bot_api.calls == ["sendDocument", "sendDocument"]

    files = asyncio.run(load_files("rec-1"))
    assert [(row.kind, row.idx) for row in files] == [("audio", 0), ("transcript", 0)]

    audio_row = files[0]
    assert audio_row.sha256 == audio_sha
    assert audio_row.size_bytes == (folder / "audio.mp3").stat().st_size
    assert bot_api.files[audio_row.tg_file_id] == (folder / "audio.mp3").read_bytes()
    caption = bot_api.messages[audio_row.tg_message_id]["caption"]
    assert caption.startswith("🎙 lecture.m4a — audio")
    assert caption.endswith(
        archive.build_caption("rec-1", "audio", 0, 1, audio_row.size_bytes, audio_sha)
    )
    assert archive.parse_caption(caption)["rid"] == "rec-1"

    recording = asyncio.run(load_recording("rec-1"))
    assert recording.storage_state == "tg"
    assert recording.archived_at is not None


def test_archive_skips_files_that_are_already_uploaded(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    asyncio.run(archive.archive_recording("rec-1"))
    second = asyncio.run(archive.archive_recording("rec-1"))

    assert second["uploaded"] == 0
    assert bot_api.calls.count("sendDocument") == 2


def test_archive_retries_after_a_flood_wait(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, with_transcript=False)
    bot_api.failures["sendDocument"] = [
        (429, {"ok": False, "description": "Too Many Requests: retry after 1", "parameters": {"retry_after": 0}})
    ]

    asyncio.run(archive.archive_recording("rec-1"))

    assert bot_api.calls.count("sendDocument") == 2
    assert bot_api.pauses.seconds == [0.0]
    assert len(asyncio.run(load_files("rec-1"))) == 1


def test_fetch_audio_glues_the_parts_back_together(monkeypatch, tmp_path, bot_api):
    limit = settings.telegram_chunk_mb * 1024 * 1024
    folder = make_recording(monkeypatch, tmp_path, audio=b"x" * (limit + 1), with_transcript=False)

    async def fake_info(path):
        return {}

    async def fake_split(path, output_dir, minutes, passthrough=False, strip_headers=False):
        output_dir.mkdir(parents=True, exist_ok=True)
        chunks = []
        for idx, payload in enumerate((b"first-part" * 1000, b"second-part" * 500)):
            chunk = output_dir / f"chunk_{idx:03d}.mp3"
            chunk.write_bytes(payload)
            chunks.append(chunk)
        return chunks

    monkeypatch.setattr(archive, "get_media_info", fake_info)
    monkeypatch.setattr(archive, "get_audio_bitrate_kbps", lambda info: 48)
    monkeypatch.setattr(archive, "split_audio", fake_split)

    asyncio.run(archive.archive_recording("rec-1"))
    assert not (folder / archive.CHUNK_DIR_NAME).exists()

    restored = tmp_path / "restored.mp3"
    asyncio.run(archive.fetch_audio("rec-1", restored))
    assert restored.read_bytes() == b"first-part" * 1000 + b"second-part" * 500


def test_expired_file_id_is_refreshed_from_the_stored_message(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    asyncio.run(archive.archive_recording("rec-1"))
    row = asyncio.run(load_files("rec-1"))[0]
    bot_api.expire(row.tg_file_id)

    restored = tmp_path / "restored.mp3"
    asyncio.run(archive.fetch_audio("rec-1", restored))

    assert "copyMessage" in bot_api.calls
    assert restored.read_bytes() == (folder / "audio.mp3").read_bytes()
    refreshed = asyncio.run(load_files("rec-1"))[0]
    assert refreshed.tg_file_id != row.tg_file_id
    assert refreshed.tg_file_id in bot_api.files


def test_verify_reports_broken_files_when_repair_is_off(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, with_transcript=False)
    asyncio.run(archive.archive_recording("rec-1"))
    row = asyncio.run(load_files("rec-1"))[0]
    bot_api.expire(row.tg_file_id)

    problems = asyncio.run(archive.verify_recording("rec-1", repair=False))
    assert len(problems) == 1
    assert "audio[0]" in problems[0]
    assert "copyMessage" not in bot_api.calls

    assert asyncio.run(archive.verify_recording("rec-1")) == []
    assert "copyMessage" in bot_api.calls


def test_restore_keeps_an_existing_local_copy(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    asyncio.run(archive.archive_recording("rec-1"))

    path, downloaded = asyncio.run(archive.restore_recording("rec-1"))
    assert (path, downloaded) == (folder / "audio.mp3", False)

    (folder / "audio.mp3").unlink()
    path, downloaded = asyncio.run(archive.restore_recording("rec-1"))
    assert downloaded is True
    assert path.read_bytes() == b"lecture" * 100

    recording = asyncio.run(load_recording("rec-1"))
    # the channel copy is still the archive, but the local file has to survive a cleanup
    assert recording.storage_state == "tg"
    assert recording.keep_local is True


def test_fetch_audio_rejects_a_truncated_download(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, with_transcript=False)
    asyncio.run(archive.archive_recording("rec-1"))
    row = asyncio.run(load_files("rec-1"))[0]
    bot_api.files[row.tg_file_id] = bot_api.files[row.tg_file_id][:10]

    with pytest.raises(telegram.TelegramError, match="bytes instead of"):
        asyncio.run(archive.fetch_audio("rec-1", tmp_path / "restored.mp3"))
    assert not (tmp_path / "restored.mp3").exists()


def test_pending_recordings_lists_only_unarchived_ones(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, name="rec-1")
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, name="rec-2")

    assert asyncio.run(archive.pending_recordings()) == ["rec-1", "rec-2"]
    asyncio.run(archive.archive_recording("rec-1"))
    assert asyncio.run(archive.pending_recordings()) == ["rec-2"]
    assert asyncio.run(archive.archived_recordings()) == ["rec-1"]

    summary = asyncio.run(archive.status_summary())
    assert summary["recordings"] == 2
    assert summary["states"]["tg"] == 1
    assert summary["files"] == 2


def test_discover_chats_reads_updates(monkeypatch, bot_api):
    bot_api.updates = [
        {"update_id": 1, "channel_post": {"message_id": 5, "chat": {"id": -1001234567890, "type": "channel", "title": "Lectures"}}},
        {"update_id": 2, "message": {"message_id": 6, "chat": {"id": 777, "type": "private", "first_name": "Andrii"}}},
    ]
    chats = asyncio.run(telegram.discover_chats())
    assert {chat["id"] for chat in chats} == {-1001234567890, 777}
    assert chats[0]["title"] == "Lectures"


def test_archive_refuses_to_run_without_configuration(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    monkeypatch.setattr(settings, "telegram_enabled", False)
    with pytest.raises(telegram.TelegramNotConfigured):
        asyncio.run(archive.archive_recording("rec-1"))


def test_send_document_masks_the_token_in_status(monkeypatch, bot_api):
    assert telegram.mask_token("123456789:AAHsecretsecret") == "12345678...cret"
    assert telegram.mask_token(None) == "-"

