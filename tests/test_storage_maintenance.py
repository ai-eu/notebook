import asyncio
import contextlib
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Recording
from app.services.storage import archive, cache
from app.services.storage.maintenance import maintenance_loop, run_maintenance
from conftest import load_files, load_recording, make_recording


def archive_now(recording_id: str = "rec-1") -> None:
    asyncio.run(archive.archive_recording(recording_id))


async def age_archive(recording_id: str, days: float) -> None:
    """Pretend the recording was copied into the channel N days ago."""
    async with AsyncSessionLocal() as db:
        recording = (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one()
        recording.archived_at = datetime.now(timezone.utc) - timedelta(days=days)
        await db.commit()


def test_retention_drops_the_audio_of_old_archived_recordings(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    archive_now()
    asyncio.run(age_archive("rec-1", settings.audio_retention_days + 1))

    result = asyncio.run(archive.cleanup_local_audio())

    assert result["removed"] == 1
    assert result["freed_bytes"] == 700
    assert not (folder / "audio.mp3").exists()
    # the transcript stays on the server
    assert (folder / "transcript.json").exists()
    assert asyncio.run(load_recording("rec-1")).storage_state == "evicted"
    assert asyncio.run(archive.cleanup_local_audio()) == {"removed": 0, "freed_bytes": 0}


def test_retention_keeps_recent_recordings(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    archive_now()

    assert asyncio.run(archive.cleanup_local_audio())["removed"] == 0
    assert (folder / "audio.mp3").exists()
    assert asyncio.run(load_recording("rec-1")).storage_state == "tg"


def test_retention_keeps_recordings_pulled_back_on_purpose(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    archive_now()
    asyncio.run(age_archive("rec-1", settings.audio_retention_days + 1))
    asyncio.run(archive.restore_recording("rec-1"))

    assert asyncio.run(archive.cleanup_local_audio())["removed"] == 0
    assert (folder / "audio.mp3").exists()


def test_retention_can_be_switched_off(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    archive_now()
    asyncio.run(age_archive("rec-1", settings.audio_retention_days + 1))
    monkeypatch.setattr(settings, "audio_retention_days", 0)

    assert asyncio.run(archive.cleanup_local_audio())["removed"] == 0
    assert (folder / "audio.mp3").exists()


def test_evicted_audio_is_pulled_back_from_the_channel(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    archive_now()
    (folder / "audio.mp3").unlink()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))

    path = asyncio.run(cache.ensure_cached("rec-1"))

    assert path == tmp_path / "cache" / "rec-1.mp3"
    assert path.read_bytes() == b"lecture" * 100
    # a second call is served from the cache, without touching Telegram again
    bot_api.calls.clear()
    assert asyncio.run(cache.ensure_cached("rec-1")) == path
    assert bot_api.calls == []


def test_evicted_audio_reports_when_the_channel_is_unreachable(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    archive_now()
    (folder / "audio.mp3").unlink()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    bot_api.failures["getFile"] = [(400, {"ok": False, "description": "Bad Request: file is not found"})] * 2
    bot_api.failures["copyMessage"] = [(400, {"ok": False, "description": "Bad Request: message to copy not found"})]

    assert asyncio.run(cache.ensure_cached("rec-1")) is None
    assert not (tmp_path / "cache" / "rec-1.mp3").exists()


def test_cache_eviction_keeps_the_most_recently_used_files(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "cache_max_mb", 1)
    folder = cache.cache_dir()
    folder.mkdir(parents=True)

    now = time.time()
    for name, age_hours in (("old.mp3", 3), ("older.mp3", 5), ("new.mp3", 1)):
        path = folder / name
        path.write_bytes(b"x" * (512 * 1024))
        os.utime(path, (now - age_hours * 3600, now - age_hours * 3600))

    result = asyncio.run(cache.cleanup_cache())

    assert result["removed"] == 1
    assert sorted(path.name for path in folder.glob("*.mp3")) == ["new.mp3", "old.mp3"]


def test_cache_eviction_is_a_no_op_when_everything_fits(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    folder = cache.cache_dir()
    folder.mkdir(parents=True)
    (folder / "one.mp3").write_bytes(b"x" * 1024)

    assert asyncio.run(cache.cleanup_cache()) == {"removed": 0, "freed_bytes": 0}
    assert (folder / "one.mp3").exists()


def test_deleting_a_recording_removes_the_messages_from_the_channel(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    archive_now()
    assert len(bot_api.messages) == 2

    assert asyncio.run(archive.delete_remote_copy("rec-1")) == 2
    assert bot_api.messages == {}


def test_delete_remote_copy_survives_a_refusing_api(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    archive_now()
    bot_api.failures["deleteMessage"] = [(400, {"ok": False, "description": "Bad Request: message can't be deleted"})] * 2

    assert asyncio.run(archive.delete_remote_copy("rec-1")) == 0


def test_maintenance_archives_pending_recordings_and_cleans_up(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, name="rec-1")
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, name="rec-2")
    archive_now("rec-1")
    asyncio.run(age_archive("rec-1", settings.audio_retention_days + 1))
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))

    summary = asyncio.run(run_maintenance())

    assert summary["archived"] == 1
    assert summary["failed"] == 0
    assert summary["removed"] == 1
    assert asyncio.run(load_recording("rec-1")).storage_state == "evicted"
    assert asyncio.run(load_recording("rec-2")).storage_state == "tg"
    assert len(asyncio.run(load_files("rec-2"))) == 2


def test_maintenance_skips_telegram_when_it_is_not_configured(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    monkeypatch.setattr(settings, "telegram_enabled", False)

    summary = asyncio.run(run_maintenance())

    assert summary == {"archived": 0, "failed": 0, "removed": 0, "freed_bytes": 0, "cache_removed": 0}
    assert bot_api.calls == []
    assert asyncio.run(load_recording("rec-1")).storage_state == "local"


def test_maintenance_keeps_going_when_one_recording_fails(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, name="rec-1")
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, name="rec-2")
    # the first upload of rec-1 fails for good, so rec-1 stays local and rec-2 is archived
    bot_api.failures["sendDocument"] = [(500, {"ok": False, "description": "Internal Server Error"})] * 3

    summary = asyncio.run(run_maintenance())

    assert summary["failed"] == 1
    assert summary["archived"] == 1
    assert asyncio.run(load_recording("rec-1")).storage_state == "local"
    assert asyncio.run(load_recording("rec-2")).storage_state == "tg"


def test_the_background_loop_archives_on_its_own(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)

    async def scenario() -> str:
        task = asyncio.create_task(maintenance_loop(interval_seconds=3600, startup_delay=0))
        try:
            for _ in range(200):
                await asyncio.sleep(0.02)
                async with AsyncSessionLocal() as db:
                    state = await db.scalar(select(Recording.storage_state))
                if state == "tg":
                    return state
            return state
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert asyncio.run(scenario()) == "tg"
