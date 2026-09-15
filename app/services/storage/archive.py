import asyncio
import hashlib
import logging
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Recording, StoredFile
from app.services.converter import (
    calculate_chunk_minutes,
    get_audio_bitrate_kbps,
    get_media_info,
    split_audio,
    split_mp3_by_size,
)
from app.services.storage import telegram
from app.utils import resolve_recording_path, safe_delete


CHUNK_DIR_NAME = "tg_chunks"
AUDIO_KIND = "audio"
# Files that stay on the server and are only backed up into the channel.
ARTIFACTS = (
    ("transcript", "transcript.json", "application/json"),
    ("formatted", "formatted.txt", "text/plain"),
)

_last_send_at = 0.0


def build_caption(recording_id: str, kind: str, idx: int, total: int, size: int, sha256: str) -> str:
    """The caption is the only index that survives a lost database."""
    return f"rid={recording_id};kind={kind};part={idx + 1}/{total};size={size};sha={sha256}"


def parse_caption(caption: str) -> dict:
    return dict(re.findall(r"(\w+)=([^;]+)", caption or ""))


def _limit_bytes() -> int:
    return settings.telegram_chunk_mb * 1024 * 1024


def _relative(path: Path) -> str:
    try:
        return path.relative_to(settings.data_dir_absolute).as_posix()
    except ValueError:
        return path.as_posix()


async def _sha256(path: Path) -> str:
    def _hash() -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    return await asyncio.to_thread(_hash)


async def _throttle() -> None:
    """Keep the upload below Telegram's ~20 messages per minute channel limit."""
    global _last_send_at
    interval = settings.telegram_send_interval_seconds
    if interval <= 0:
        return
    wait = _last_send_at + interval - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    _last_send_at = time.monotonic()


async def plan_parts(folder: Path, audio_path: Path) -> list[Path]:
    """Split audio.mp3 into parts small enough for Telegram to hand back later."""
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if audio_path.stat().st_size <= _limit_bytes():
        return [audio_path]

    # Cutting at frame boundaries keeps the parts byte-exact, so gluing them back
    # reproduces the file the user uploaded.
    parts = await asyncio.to_thread(
        split_mp3_by_size, audio_path, folder / CHUNK_DIR_NAME, _limit_bytes()
    )
    if parts:
        return parts

    # Anything that is not a plain MPEG Layer III stream falls back to ffmpeg.
    info = await get_media_info(audio_path)
    bitrate = get_audio_bitrate_kbps(info) or settings.target_mp3_kbps
    minutes = calculate_chunk_minutes(bitrate, settings.telegram_chunk_mb, settings.chunk_max_minutes)
    return await split_audio(
        audio_path,
        folder / CHUNK_DIR_NAME,
        minutes,
        passthrough=True,
        strip_headers=True,
    )


async def _stored_files(db, recording_id: str) -> dict[tuple[str, int], StoredFile]:
    rows = (
        await db.execute(select(StoredFile).where(StoredFile.recording_id == recording_id))
    ).scalars().all()
    return {(row.kind, row.idx): row for row in rows}


async def _upload_part(
    db,
    recording: Recording,
    path: Path,
    *,
    kind: str,
    idx: int,
    total: int,
    mime_type: str,
    existing: StoredFile | None = None,
) -> StoredFile:
    size = path.stat().st_size
    digest = await _sha256(path)
    caption = build_caption(recording.recording_id, kind, idx, total, size, digest)
    message = await telegram.send_document(path, caption=caption, mime_type=mime_type)

    document = message.get("document") or message.get("audio") or {}
    file_id = document.get("file_id")
    if not file_id:
        raise telegram.TelegramError(f"sendDocument returned no file_id for {path.name}")

    row = existing or StoredFile(recording_id=recording.recording_id, kind=kind, idx=idx)
    row.local_path = _relative(path)
    row.size_bytes = size
    row.sha256 = digest
    row.tg_chat_id = str((message.get("chat") or {}).get("id") or settings.telegram_chat_id or "")
    row.tg_message_id = message.get("message_id")
    row.tg_file_id = file_id
    row.tg_file_unique_id = document.get("file_unique_id")
    row.uploaded_at = datetime.now(timezone.utc)
    db.add(row)
    await db.commit()
    await _throttle()
    return row


async def archive_recording(recording_id: str, *, force: bool = False) -> dict:
    """Copy the audio and the transcript files of a recording into the channel.

    Idempotent: parts that are already uploaded are skipped, so an interrupted
    upload can simply be started again.
    """
    telegram.require_configuration()
    async with AsyncSessionLocal() as db:
        recording = (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one_or_none()
        if not recording:
            raise ValueError(f"Recording {recording_id} not found")

        folder = resolve_recording_path(recording.folder_path)
        existing = await _stored_files(db, recording_id)
        parts = await plan_parts(folder, folder / "audio.mp3")

        uploaded = 0
        try:
            for idx, part in enumerate(parts):
                if not force and (AUDIO_KIND, idx) in existing:
                    continue
                await _upload_part(
                    db,
                    recording,
                    part,
                    kind=AUDIO_KIND,
                    idx=idx,
                    total=len(parts),
                    mime_type="audio/mpeg",
                    existing=existing.get((AUDIO_KIND, idx)),
                )
                uploaded += 1

            for kind, name, mime_type in ARTIFACTS:
                path = folder / name
                if not path.exists() or (not force and (kind, 0) in existing):
                    continue
                await _upload_part(
                    db,
                    recording,
                    path,
                    kind=kind,
                    idx=0,
                    total=1,
                    mime_type=mime_type,
                    existing=existing.get((kind, 0)),
                )
                uploaded += 1
        except Exception:
            if uploaded or await _has_remote_files(db, recording_id):
                recording.storage_state = "partial"
                await db.commit()
            raise

        recording.storage_state = "tg"
        recording.archived_at = datetime.now(timezone.utc)
        await db.commit()

    # The upload parts are derived data: audio.mp3 can be split again if needed.
    safe_delete(folder / CHUNK_DIR_NAME)
    return {"recording_id": recording_id, "parts": len(parts), "uploaded": uploaded}


async def _has_remote_files(db, recording_id: str) -> bool:
    count = await db.scalar(
        select(func.count()).select_from(StoredFile).where(StoredFile.recording_id == recording_id)
    )
    return bool(count)


async def _refresh_file_id(db, row: StoredFile) -> None:
    """Telegram file_ids expire; the stored message id lets us mint a fresh one.

    copyMessage posts a second copy of the file into the channel. The copy is kept
    on purpose: it is the newest message that still holds the file, so it becomes
    the pointer used by the next repair.
    """
    if not row.tg_message_id:
        raise telegram.TelegramError(f"no message id stored for {row.recording_id} {row.kind}[{row.idx}]")
    copy = await telegram.copy_message(row.tg_message_id, from_chat_id=row.tg_chat_id or None)
    document = copy.get("document") or copy.get("audio") or {}
    file_id = document.get("file_id")
    if not file_id:
        raise telegram.TelegramError("copyMessage returned no file_id")
    logging.warning(
        "Refreshed file_id of %s %s[%s] via message %s",
        row.recording_id,
        row.kind,
        row.idx,
        copy.get("message_id"),
    )
    row.tg_file_id = file_id
    row.tg_file_unique_id = document.get("file_unique_id")
    row.tg_message_id = copy.get("message_id") or row.tg_message_id
    row.uploaded_at = datetime.now(timezone.utc)
    await db.commit()


async def _fetch_part(db, row: StoredFile, dest: Path) -> None:
    try:
        file_path = await telegram.get_file(row.tg_file_id or "")
    except telegram.TelegramError as exc:
        logging.warning("file_id of %s %s[%s] is not usable (%s)", row.recording_id, row.kind, row.idx, exc)
        await _refresh_file_id(db, row)
        file_path = await telegram.get_file(row.tg_file_id or "")
    await telegram.download(file_path, dest)

    label = f"{row.recording_id} {row.kind}[{row.idx}]"
    if row.size_bytes and dest.stat().st_size != row.size_bytes:
        raise telegram.TelegramError(f"{label}: got {dest.stat().st_size} bytes instead of {row.size_bytes}")
    if row.sha256 and await _sha256(dest) != row.sha256:
        raise telegram.TelegramError(f"{label}: checksum mismatch")


def _glue(parts: list[Path], dest: Path) -> None:
    """Concatenate the parts; the result appears only when it is complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    with partial.open("wb") as out:
        for part in parts:
            with part.open("rb") as handle:
                shutil.copyfileobj(handle, out, 1024 * 1024)
    partial.replace(dest)


async def fetch_audio(recording_id: str, dest: Path) -> Path:
    """Download every audio part of a recording and glue them back into dest."""
    telegram.require_configuration()
    tmp_dir = Path(tempfile.mkdtemp(prefix="tg-fetch-"))
    try:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(StoredFile)
                    .where(StoredFile.recording_id == recording_id, StoredFile.kind == AUDIO_KIND)
                    .order_by(StoredFile.idx)
                )
            ).scalars().all()
            if not rows:
                raise telegram.TelegramError(f"no archived audio for {recording_id}")
            parts = []
            for row in rows:
                part_path = tmp_dir / f"part_{row.idx:03d}.mp3"
                await _fetch_part(db, row, part_path)
                parts.append(part_path)
        await asyncio.to_thread(_glue, parts, dest)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return dest


async def restore_recording(recording_id: str, *, force: bool = False) -> tuple[Path, bool]:
    """Pull the audio of a recording back into its folder.

    The recording stays archived; `keep_local` tells the cleanup that the local copy
    was asked for on purpose.
    """
    async with AsyncSessionLocal() as db:
        recording = (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one_or_none()
        if not recording:
            raise ValueError(f"Recording {recording_id} not found")
        dest = resolve_recording_path(recording.folder_path) / "audio.mp3"

    if dest.exists() and dest.stat().st_size > 0 and not force:
        downloaded = False
    else:
        await fetch_audio(recording_id, dest)
        downloaded = True

    async with AsyncSessionLocal() as db:
        recording = (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one_or_none()
        if recording:
            recording.keep_local = True
            await db.commit()
    return dest, downloaded


async def verify_recording(recording_id: str, *, repair: bool = True) -> list[str]:
    """Check that every stored file can still be fetched; refresh expired file_ids."""
    telegram.require_configuration()
    problems: list[str] = []
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(StoredFile)
                .where(StoredFile.recording_id == recording_id)
                .order_by(StoredFile.kind, StoredFile.idx)
            )
        ).scalars().all()
        if not rows:
            return [f"{recording_id}: nothing archived"]
        for row in rows:
            label = f"{recording_id} {row.kind}[{row.idx}]"
            try:
                await telegram.get_file(row.tg_file_id or "")
            except telegram.TelegramError as exc:
                if not repair:
                    problems.append(f"{label}: {exc}")
                    continue
                try:
                    await _refresh_file_id(db, row)
                except telegram.TelegramError as repair_exc:
                    problems.append(f"{label}: {exc} (repair failed: {repair_exc})")
    return problems


async def pending_recordings(limit: int | None = None) -> list[str]:
    """Recordings that still need to be copied into the channel."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Recording.recording_id, Recording.folder_path)
                .where(
                    Recording.status == "done",
                    or_(Recording.storage_state.is_(None), Recording.storage_state.in_(("local", "partial"))),
                )
                .order_by(Recording.created_at)
            )
        ).all()
    pending = [
        recording_id
        for recording_id, folder_path in rows
        if (resolve_recording_path(folder_path) / "audio.mp3").exists()
    ]
    return pending[:limit] if limit else pending


async def archived_recordings() -> list[str]:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Recording.recording_id).where(Recording.storage_state.in_(("tg", "partial", "evicted")))
            )
        ).all()
    return [row[0] for row in rows]


async def status_summary() -> dict:
    async with AsyncSessionLocal() as db:
        recordings = await db.scalar(select(func.count()).select_from(Recording))
        states = (
            await db.execute(
                select(Recording.storage_state, func.count()).group_by(Recording.storage_state)
            )
        ).all()
        files = await db.scalar(select(func.count()).select_from(StoredFile))
        bytes_remote = await db.scalar(select(func.coalesce(func.sum(StoredFile.size_bytes), 0)))
    return {
        "recordings": recordings or 0,
        "states": {state or "local": count for state, count in states},
        "files": files or 0,
        "bytes_remote": int(bytes_remote or 0),
    }


async def delete_remote_copy(recording_id: str) -> int:
    """Drop the messages of a recording from the channel.

    Best effort: a message Telegram refuses to delete (or a network hiccup) must not
    block the deletion of the recording itself.
    """
    if not telegram.is_configured():
        return 0
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(select(StoredFile).where(StoredFile.recording_id == recording_id))
        ).scalars().all()

    removed = 0
    for row in rows:
        if not row.tg_message_id:
            continue
        try:
            await telegram.delete_message(row.tg_message_id, chat_id=row.tg_chat_id or None)
            removed += 1
        except telegram.TelegramError as exc:
            logging.warning("Could not delete message %s of %s: %s", row.tg_message_id, recording_id, exc)
    return removed


async def cleanup_local_audio(*, now: datetime | None = None, dry_run: bool = False) -> dict:
    """Drop the audio of recordings that have been in the channel for longer than the retention.

    Transcripts stay on the server, and `keep_local` protects recordings that were
    pulled back with `tg-restore`.
    """
    days = settings.audio_retention_days
    if days <= 0:
        return {"removed": 0, "freed_bytes": 0}
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Recording).where(
                    Recording.storage_state == "tg",
                    Recording.keep_local.is_(False),
                    Recording.archived_at.is_not(None),
                    Recording.archived_at < cutoff,
                )
            )
        ).scalars().all()

        removed = 0
        freed = 0
        for recording in rows:
            folder = resolve_recording_path(recording.folder_path)
            audio = folder / "audio.mp3"
            if audio.exists():
                freed += audio.stat().st_size
                removed += 1
                if not dry_run:
                    safe_delete(audio)
            if not dry_run:
                # The chunks were only needed for the upload.
                safe_delete(folder / "chunks")
                safe_delete(folder / CHUNK_DIR_NAME)
                recording.storage_state = "evicted"
        if not dry_run:
            await db.commit()

    if removed and not dry_run:
        logging.info("Dropped %s local audio file(s), freed %s bytes", removed, freed)
    return {"removed": removed, "freed_bytes": freed}
