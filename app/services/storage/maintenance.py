import asyncio
import logging

from app.services.storage import telegram
from app.services.storage.archive import (
    archive_recording,
    cleanup_local_audio,
    pending_recordings,
)
from app.services.storage.cache import cleanup_cache


async def run_maintenance() -> dict:
    """Catch up on uploads and drop local audio that is safely stored in the channel."""
    summary = {"archived": 0, "failed": 0, "removed": 0, "freed_bytes": 0, "cache_removed": 0}
    if telegram.is_configured():
        for recording_id in await pending_recordings():
            try:
                await archive_recording(recording_id)
                summary["archived"] += 1
            except Exception as exc:
                summary["failed"] += 1
                logging.warning("Archiving %s failed: %s", recording_id, exc)

        cleanup = await cleanup_local_audio()
        summary["removed"] = cleanup["removed"]
        summary["freed_bytes"] = cleanup["freed_bytes"]

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
