import asyncio
import logging

from app.services.storage import telegram
from app.services.storage.archive import (
    archive_artifacts,
    archive_recording,
    cleanup_local_audio,
    pending_artifact_recordings,
    pending_recordings,
    process_remote_cleanup,
)
from app.services.storage.cache import cleanup_cache


async def run_maintenance() -> dict:
    """Catch up on uploads, evict old local files and clean the cache."""
    summary = {
        "archived": 0,
        "artifacts": 0,
        "failed": 0,
        "removed": 0,
        "freed_bytes": 0,
        "remote_deleted": 0,
        "remote_manual": 0,
        "cache_removed": 0,
    }
    if telegram.is_configured():
        for recording_id in await pending_recordings():
            try:
                await archive_recording(recording_id)
                summary["archived"] += 1
            except Exception as exc:
                summary["failed"] += 1
                logging.warning("Archiving %s failed: %s", recording_id, exc)

        for recording_id in await pending_artifact_recordings():
            try:
                await archive_artifacts(recording_id)
                summary["artifacts"] += 1
            except Exception as exc:
                summary["failed"] += 1
                logging.warning("Archiving artifacts of %s failed: %s", recording_id, exc)

        cleanup = await cleanup_local_audio()
        summary["removed"] = cleanup["removed"]
        summary["freed_bytes"] = cleanup["freed_bytes"]

        remote = await process_remote_cleanup()
        summary["remote_deleted"] = remote["deleted"]
        summary["remote_manual"] = remote["manual"]

    summary["cache_removed"] = (await cleanup_cache())["removed"]
    return summary


async def maintenance_loop(interval_seconds: float = 6 * 3600, startup_delay: float = 5.0) -> None:
    """Periodically archive new recordings and clean up; runs for the lifetime of the app."""
    await asyncio.sleep(startup_delay)
    while True:
        try:
            await run_maintenance()
        except Exception:
            logging.exception("Telegram maintenance pass failed")
        await asyncio.sleep(interval_seconds)
