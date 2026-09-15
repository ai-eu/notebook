import asyncio
import hashlib

import pytest

from app.config import settings
from app.services.converter import mp3_frame_length, split_mp3_by_size
from app.services.storage import archive
from conftest import make_recording


def fake_mp3(frames: int) -> bytes:
    """A stream of MPEG-1 Layer III frames (128 kbps, 44100 Hz, 417 bytes each)."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    return frame * frames


def test_mp3_frame_length_reads_a_real_header():
    assert mp3_frame_length(b"\xff\xfb\x90\x00") == 417
    # MPEG-2 Layer III, 64 kbps, 22050 Hz: 576 / 8 * 64000 / 22050 = 208 bytes
    assert mp3_frame_length(b"\xff\xf3\x80\x00") == 208
    assert mp3_frame_length(b"\x00\x00\x00\x00") == 0
    assert mp3_frame_length(b"\xff\xfb") == 0


def test_split_mp3_by_size_keeps_the_stream_intact(tmp_path):
    source = tmp_path / "audio.mp3"
    payload = fake_mp3(100)
    source.write_bytes(payload)

    parts = split_mp3_by_size(source, tmp_path / "parts", 10_000)

    assert len(parts) == 5
    assert all(part.stat().st_size <= 10_000 for part in parts)
    assert b"".join(part.read_bytes() for part in parts) == payload


def test_split_mp3_by_size_keeps_an_id3_tag_in_the_first_part(tmp_path):
    source = tmp_path / "audio.mp3"
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x64" + b"\x00" * 100
    payload = tag + fake_mp3(60)
    source.write_bytes(payload)

    parts = split_mp3_by_size(source, tmp_path / "parts", 10_000)

    assert parts[0].read_bytes().startswith(b"ID3")
    assert b"".join(part.read_bytes() for part in parts) == payload


def test_split_mp3_by_size_gives_up_on_a_foreign_file(tmp_path):
    source = tmp_path / "audio.mp3"
    source.write_bytes(b"\x00" * 50_000)

    assert split_mp3_by_size(source, tmp_path / "parts", 10_000) is None


def test_large_audio_is_split_at_frame_boundaries(monkeypatch, tmp_path, bot_api):
    limit = settings.telegram_chunk_mb * 1024 * 1024
    payload = fake_mp3(60_000)  # ~24 MiB
    folder = make_recording(monkeypatch, tmp_path, audio=payload)

    parts = asyncio.run(archive.plan_parts(folder, folder / "audio.mp3"))

    assert len(parts) == 2
    assert all(part.stat().st_size <= limit for part in parts)
    assert b"".join(part.read_bytes() for part in parts) == payload


def test_a_foreign_stream_falls_back_to_ffmpeg(monkeypatch, tmp_path, bot_api):
    limit = settings.telegram_chunk_mb * 1024 * 1024
    folder = make_recording(monkeypatch, tmp_path, audio=b"\x00" * (limit + 1))
    captured = {}

    async def fake_info(path):
        return {}

    async def fake_split(path, output_dir, minutes, passthrough=False, strip_headers=False):
        captured["passthrough"] = passthrough
        captured["strip_headers"] = strip_headers
        output_dir.mkdir(parents=True, exist_ok=True)
        chunk = output_dir / "chunk_000.mp3"
        chunk.write_bytes(b"z" * 1024)
        return [chunk]

    monkeypatch.setattr(archive, "get_media_info", fake_info)
    monkeypatch.setattr(archive, "get_audio_bitrate_kbps", lambda info: 48)
    monkeypatch.setattr(archive, "split_audio", fake_split)

    parts = asyncio.run(archive.plan_parts(folder, folder / "audio.mp3"))

    assert [part.name for part in parts] == ["chunk_000.mp3"]
    # a stream copy without headers, so the parts can still be glued together
    assert captured == {"passthrough": True, "strip_headers": True}


def test_parts_can_be_glued_back_into_the_original(monkeypatch, tmp_path, bot_api):
    limit = settings.telegram_chunk_mb * 1024 * 1024
    payload = fake_mp3(60_000)
    folder = make_recording(monkeypatch, tmp_path, audio=payload, with_transcript=False)

    asyncio.run(archive.archive_recording("rec-1"))
    (folder / "audio.mp3").unlink()

    restored = tmp_path / "restored.mp3"
    asyncio.run(archive.fetch_audio("rec-1", restored))

    assert restored.read_bytes() == payload
    assert hashlib.sha256(restored.read_bytes()).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_a_single_part_is_uploaded_without_splitting(monkeypatch, tmp_path, bot_api):
    payload = fake_mp3(10)
    folder = make_recording(monkeypatch, tmp_path, audio=payload)

    async def no_ffprobe(path):
        raise AssertionError("a small file must not be probed")

    monkeypatch.setattr(archive, "get_media_info", no_ffprobe)
    parts = asyncio.run(archive.plan_parts(folder, folder / "audio.mp3"))

    assert parts == [folder / "audio.mp3"]
