import asyncio
import logging
from pathlib import Path

from app.config import settings
from app.services.storage import telegram
from app.services.storage.archive import fetch_audio


def cache_dir() -> Path:
    return settings.data_dir_absolute / settings.cache_dir


def cache_path(recording_id: str) -> Path:
    return cache_dir() / f"{recording_id}.mp3"


# One lock per recording, so two players opening the same lecture do not download it twice.
_locks: dict[str, asyncio.Lock] = {}


async def ensure_cached(recording_id: str) -> Path | None:
    """Local file for a recording, pulled back from the channel when it is not here.

    Returns None when the recording has no local copy and cannot be fetched.
    """
    path = cache_path(recording_id)
    if path.exists() and path.stat().st_size > 0:
        _touch(path)
        return path
    if not telegram.is_configured():
        return None

    lock = _locks.setdefault(recording_id, asyncio.Lock())
    async with lock:
        if path.exists() and path.stat().st_size > 0:
            _touch(path)
            return path
        try:
            await fetch_audio(recording_id, path)
        except (telegram.TelegramError, OSError) as exc:
            logging.warning("Could not pull %s back from the archive: %s", recording_id, exc)
            return None
    return path


def drop(recording_id: str) -> None:
    cache_path(recording_id).unlink(missing_ok=True)


def _touch(path: Path) -> None:
    """Mark the entry as recently used; the cleanup drops the oldest files first."""
    try:
        path.touch()
    except OSError:
        pass


def cache_stats() -> dict:
    folder = cache_dir()
    entries = [path for path in folder.glob("*.mp3") if path.is_file()] if folder.is_dir() else []
    return {"files": len(entries), "bytes": sum(path.stat().st_size for path in entries)}


async def cleanup_cache() -> dict:
    """Keep the cache under CACHE_MAX_MB, dropping the least recently used entries."""
    limit = settings.cache_max_mb * 1024 * 1024
    folder = cache_dir()
    if limit <= 0 or not folder.is_dir():
        return {"removed": 0, "freed_bytes": 0}

    entries = [path for path in folder.glob("*.mp3") if path.is_file()]
    entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    total = sum(path.stat().st_size for path in entries)

    removed = 0
    freed = 0
    for path in reversed(entries):
        if total <= limit:
            break
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError as exc:
            logging.warning("Could not remove cached file %s: %s", path, exc)
            continue
        total -= size
        removed += 1
        freed += size
    return {"removed": removed, "freed_bytes": freed}
