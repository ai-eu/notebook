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
from app.models import Recording, RecordingArtifact, RemoteCleanup, StoredFile
from app.services.converter import (
    calculate_chunk_minutes,
    get_audio_bitrate_kbps,
    get_media_info,
    split_audio,
    split_mp3_by_size,
)
from app.services.storage import telegram
from app.services.storage.artifacts import (
    KIND_FORMATTED,
    KIND_SENTENCES,
    KIND_TRANSCRIPT,
    ensure_artifacts,
    translation_kind,
)
from app.utils import resolve_recording_path, safe_delete


CHUNK_DIR_NAME = "tg_chunks"
AUDIO_KIND = "audio"
# Files that stay on the server and are only backed up into the channel.
ARTIFACTS = (
    (KIND_TRANSCRIPT, "transcript.json", "application/json"),
    (KIND_SENTENCES, "sentences.json", "application/json"),
    (KIND_FORMATTED, "formatted.txt", "text/plain"),
)


_last_send_at = 0.0


KIND_LABELS = {
    "audio": "audio",
    "transcript": "transcript",
    "sentences": "sentences",
    "formatted": "formatted text",
}


def _human_label(recording: Recording, kind: str, idx: int, total: int) -> str:
    """A readable first caption line: what the file is, which recording it belongs to."""
    name = (recording.original_filename or recording.recording_id).replace("\n", " ").replace(";", ",").strip()
    kind_label = KIND_LABELS.get(kind, kind)
    part = f" part {idx + 1}/{total}" if total > 1 else ""
    bits = [f"🎙 {name} — {kind_label}{part}"]
    if recording.duration:
        seconds = int(recording.duration)
        bits.append(f"{seconds // 60}:{seconds % 60:02d}" if seconds < 3600 else f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}")
    if recording.created_at:
        bits.append(recording.created_at.strftime("%Y-%m-%d"))
    return " · ".join(bits)


def build_caption(
    recording_id: str,
    kind: str,
    idx: int,
    total: int,
    size: int,
    sha256: str,
    label: str | None = None,
    *,
    uid: int | None = None,
    version: int | None = None,
) -> str:
    """The metadata line is the only index that survives a lost database.

    It goes last so a stray ``key=value`` inside the human-readable label can never
    shadow the real fields in parse_caption (later matches win).
    """
    meta = f"rid={recording_id};kind={kind};part={idx + 1}/{total};size={size};sha={sha256}"
    if uid is not None:
        meta += f";uid={uid}"
    if version is not None:
        meta += f";ver={version}"
    return f"{label}\n{meta}" if label else meta


def parse_caption(caption: str) -> dict:
    # [^;\n] keeps a value from spilling onto the metadata line when the caption
    # has a human-readable first line containing "key=value"-looking text.
    return dict(re.findall(r"(\w+)=([^;\n]+)", caption or ""))


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


async def _enqueue_cleanup_row(db, row: StoredFile, recording_id: str) -> None:
    """Remember a message that must be deleted after a file is replaced/removed."""
    if not row.tg_message_id:
        return
    db.add(
        RemoteCleanup(
            recording_id=recording_id,
            chat_id=row.tg_chat_id,
            message_id=row.tg_message_id,
            sent_at=row.uploaded_at,
        )
    )
    await db.commit()


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
    version: int | None = None,
) -> StoredFile:
    size = path.stat().st_size
    digest = await _sha256(path)
    caption = build_caption(
        recording.recording_id,
        kind,
        idx,
        total,
        size,
        digest,
        label=_human_label(recording, kind, idx, total),
        uid=recording.user_id,
        version=version,
    )
    message = await telegram.send_document(path, caption=caption, mime_type=mime_type)

    document = message.get("document") or message.get("audio") or {}
    file_id = document.get("file_id")
    if not file_id:
        raise telegram.TelegramError(f"sendDocument returned no file_id for {path.name}")
    remote_size = document.get("file_size")
    if remote_size is not None and int(remote_size) != size:
        raise telegram.TelegramError(
            f"{path.name}: Telegram reports {remote_size} bytes instead of {size}"
        )

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


async def _upload_content(
    db,
    recording: Recording,
    *,
    kind: str,
    filename: str,
    content: str,
    mime_type: str,
    existing: StoredFile | None = None,
    version: int | None = None,
) -> tuple[StoredFile, bool]:
    """Upload a text artifact from SQLite; returns (row, uploaded)."""
    payload = content.encode("utf-8")
    size = len(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if (
        existing is not None
        and existing.sha256 == digest
        and existing.size_bytes == size
    ):
        return existing, False

    old_message_id = existing.tg_message_id if existing is not None else None
    old_chat_id = existing.tg_chat_id if existing is not None else None
    old_uploaded_at = existing.uploaded_at if existing is not None else None

    caption = build_caption(
        recording.recording_id,
        kind,
        0,
        1,
        size,
        digest,
        label=_human_label(recording, kind, 0, 1),
        uid=recording.user_id,
        version=version,
    )
    message = await telegram.send_document_bytes(
        filename, payload, caption=caption, mime_type=mime_type
    )
    document = message.get("document") or message.get("audio") or {}
    file_id = document.get("file_id")
    if not file_id:
        raise telegram.TelegramError(f"sendDocument returned no file_id for {filename}")
    remote_size = document.get("file_size")
    if remote_size is not None and int(remote_size) != size:
        raise telegram.TelegramError(
            f"{filename}: Telegram reports {remote_size} bytes instead of {size}"
        )

    row = existing or StoredFile(recording_id=recording.recording_id, kind=kind, idx=0)
    row.local_path = None
    row.size_bytes = size
    row.sha256 = digest
    row.tg_chat_id = str((message.get("chat") or {}).get("id") or settings.telegram_chat_id or "")
    row.tg_message_id = message.get("message_id")
    row.tg_file_id = file_id
    row.tg_file_unique_id = document.get("file_unique_id")
    row.uploaded_at = datetime.now(timezone.utc)
    db.add(row)
    if old_message_id:
        # Queue the old version only after the replacement was stored successfully.
        db.add(
            RemoteCleanup(
                recording_id=recording.recording_id,
                chat_id=old_chat_id,
                message_id=old_message_id,
                sent_at=old_uploaded_at,
            )
        )
    await db.commit()
    await _throttle()
    return row, True


async def archive_audio(recording_id: str, *, force: bool = False) -> dict:
    """Upload the audio parts. Safe to run again after an interruption."""
    telegram.require_configuration()
    folder: Path | None = None
    uploaded = 0
    parts_count = 0

    async with AsyncSessionLocal() as db:
        recording = (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one_or_none()
        if not recording:
            raise ValueError(f"Recording {recording_id} not found")

        folder = resolve_recording_path(recording.folder_path)
        existing = await _stored_files(db, recording_id)
        audio_path = folder / "audio.mp3"

        if not audio_path.exists():
            audio_rows = [row for (kind, _), row in existing.items() if kind == AUDIO_KIND]
            if audio_rows:
                recording.archive_verified_at = datetime.now(timezone.utc)
                await db.commit()
                safe_delete(folder / CHUNK_DIR_NAME)
                return {"recording_id": recording_id, "parts": len(audio_rows), "uploaded": 0}
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        parts = await plan_parts(folder, audio_path)
        parts_count = len(parts)
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
        except Exception:
            if uploaded or await _has_remote_files(db, recording_id):
                recording.storage_state = "partial"
                await db.commit()
            raise

        recording.archive_verified_at = datetime.now(timezone.utc)
        await db.commit()

    if folder is not None:
        # The upload parts are derived data: audio.mp3 can be split again if needed.
        safe_delete(folder / CHUNK_DIR_NAME)
    return {"recording_id": recording_id, "parts": parts_count, "uploaded": uploaded}


async def archive_artifacts(recording_id: str, *, force: bool = False) -> dict:
    """Upload transcript/formatted/translation artifacts stored in SQLite."""
    telegram.require_configuration()
    uploaded = 0

    async with AsyncSessionLocal() as db:
        recording = (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one_or_none()
        if not recording:
            raise ValueError(f"Recording {recording_id} not found")

        artifacts = await ensure_artifacts(db, recording)
        existing = await _stored_files(db, recording_id)

        for kind, name, mime_type in ARTIFACTS:
            content = artifacts.get(kind)
            if content is None:
                continue
            _, was_uploaded = await _upload_content(
                db,
                recording,
                kind=kind,
                filename=name,
                content=content,
                mime_type=mime_type,
                existing=existing.get((kind, 0)),
            )
            uploaded += int(was_uploaded)

        for lang in settings.translation_target_languages:
            kind = translation_kind(lang)
            content = artifacts.get(kind)
            if content is None:
                continue
            _, was_uploaded = await _upload_content(
                db,
                recording,
                kind=kind,
                filename=f"translation-{lang}.json",
                content=content,
                mime_type="application/json",
                existing=existing.get((kind, 0)),
            )
            uploaded += int(was_uploaded)

    return {"recording_id": recording_id, "uploaded": uploaded}


async def archive_recording(recording_id: str, *, force: bool = False) -> dict:
    """Copy the audio and text artifacts of a recording into the channel.

    Audio and text are uploaded independently: a recording whose audio is already
    evicted can still archive a new translation or a re-rendered text.
    """
    audio_result = await archive_audio(recording_id, force=force)
    try:
        artifact_result = await archive_artifacts(recording_id, force=force)
    except Exception:
        async with AsyncSessionLocal() as db:
            recording = (
                await db.execute(select(Recording).where(Recording.recording_id == recording_id))
            ).scalar_one_or_none()
            if recording:
                recording.storage_state = "partial"
                await db.commit()
        raise

    async with AsyncSessionLocal() as db:
        recording = (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one_or_none()
        if recording:
            recording.storage_state = "tg"
            recording.archived_at = datetime.now(timezone.utc)
            await db.commit()

    return {
        "recording_id": recording_id,
        "parts": audio_result["parts"],
        "uploaded": audio_result["uploaded"] + artifact_result["uploaded"],
    }


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
    # The old message is no longer the pointer; queue it for best-effort deletion.
    await _enqueue_cleanup_row(db, row, row.recording_id)
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


async def pending_artifact_recordings(limit: int | None = None) -> list[str]:
    """Recordings whose text artifacts are in SQLite but not yet in the channel."""
    kinds = [KIND_TRANSCRIPT, KIND_SENTENCES, KIND_FORMATTED]
    kinds.extend(translation_kind(lang) for lang in settings.translation_target_languages)

    async with AsyncSessionLocal() as db:
        artifact_rows = (
            await db.execute(
                select(RecordingArtifact.recording_id, RecordingArtifact.kind).where(
                    RecordingArtifact.kind.in_(kinds)
                )
            )
        ).all()
        stored_rows = (
            await db.execute(
                select(StoredFile.recording_id, StoredFile.kind).where(
                    StoredFile.kind.in_(kinds)
                )
            )
        ).all()
        done_ids = set(
            (
                await db.execute(
                    select(Recording.recording_id).where(Recording.status == "done")
                )
            ).scalars()
        )

    stored = {(recording_id, kind) for recording_id, kind in stored_rows}
    pending = sorted(
        {
            recording_id
            for recording_id, kind in artifact_rows
            if recording_id in done_ids and (recording_id, kind) not in stored
        }
    )
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


async def enqueue_remote_cleanup(recording_id: str) -> int:
    """Remember every channel message of a recording before its DB row disappears."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(StoredFile).where(StoredFile.recording_id == recording_id)
            )
        ).scalars().all()
        queued = 0
        for row in rows:
            if not row.tg_message_id:
                continue
            db.add(
                RemoteCleanup(
                    recording_id=recording_id,
                    chat_id=row.tg_chat_id,
                    message_id=row.tg_message_id,
                    sent_at=row.uploaded_at,
                )
            )
            queued += 1
        await db.commit()
    return queued


async def enqueue_artifact_cleanup(recording_id: str, kind: str) -> int:
    """Remove one archived artifact (e.g. a deleted translation) from the channel."""
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(StoredFile).where(
                    StoredFile.recording_id == recording_id,
                    StoredFile.kind == kind,
                )
            )
        ).scalars().all()
        for row in rows:
            if row.tg_message_id:
                db.add(
                    RemoteCleanup(
                        recording_id=recording_id,
                        chat_id=row.tg_chat_id,
                        message_id=row.tg_message_id,
                        sent_at=row.uploaded_at,
                    )
                )
            await db.delete(row)
        await db.commit()
        return len(rows)


async def process_remote_cleanup(limit: int = 50) -> dict:
    """Delete queued channel messages; old messages become manual cleanup items."""
    summary = {"deleted": 0, "failed": 0, "manual": 0}
    if not telegram.is_configured():
        return summary

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(RemoteCleanup)
                .where(RemoteCleanup.status == "pending")
                .order_by(RemoteCleanup.id)
                .limit(limit)
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)
        for row in rows:
            try:
                await telegram.delete_message(
                    row.message_id, chat_id=row.chat_id or None, retries=1
                )
            except telegram.TelegramError as exc:
                row.attempts += 1
                row.last_error = str(exc)[:500]
                sent_at = row.sent_at
                if sent_at is not None and sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)
                if sent_at is not None and now - sent_at > timedelta(hours=47):
                    row.status = "manual"
                    summary["manual"] += 1
                else:
                    summary["failed"] += 1
                await db.commit()
                continue

            await db.delete(row)
            await db.commit()
            summary["deleted"] += 1

    return summary


async def list_remote_orphans() -> list[RemoteCleanup]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(RemoteCleanup)
                    .where(RemoteCleanup.status == "manual")
                    .order_by(RemoteCleanup.id)
                )
            ).scalars()
        )


def _evict_local_files(recording: Recording, *, dry_run: bool = False) -> tuple[int, int]:
    """Remove working files and legacy text; the DB and Telegram stay intact."""
    folder = resolve_recording_path(recording.folder_path)
    files = [
        folder / "audio.mp3",
        folder / "original.tmp",
        folder / "transcript.json",
        folder / "formatted.txt",
        folder / "formatted-smart.txt",
    ]
    files.extend(folder / f"formatted-{lang}.txt" for lang in settings.translation_target_languages)
    directories = [folder / "chunks", folder / CHUNK_DIR_NAME]

    removed = 0
    freed = 0
    for path in files:
        if not path.is_file():
            continue
        freed += path.stat().st_size
        removed += 1
        if not dry_run:
            safe_delete(path)
    for path in directories:
        if not path.is_dir():
            continue
        removed += sum(1 for item in path.rglob("*") if item.is_file())
        if not dry_run:
            safe_delete(path)
    if not dry_run:
        try:
            folder.rmdir()
        except OSError:
            pass
    return removed, freed


async def cleanup_local_audio(*, now: datetime | None = None, dry_run: bool = False) -> dict:
    """Evict local files after a verified archive and the retention window.

    Text is already in SQLite, so only the audio has to be pulled back into the
    cache on demand. ``keep_local`` protects a copy restored on purpose.
    """
    days = settings.effective_local_retention_days
    if days < 0:
        return {"removed": 0, "freed_bytes": 0}
    if not telegram.is_configured():
        return {"removed": 0, "freed_bytes": 0}
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(0, days))

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Recording).where(
                    Recording.storage_state == "tg",
                    Recording.keep_local.is_(False),
                    Recording.archive_verified_at.is_not(None),
                    Recording.archived_at.is_not(None),
                    Recording.archived_at < cutoff,
                )
            )
        ).scalars().all()

        removed = 0
        freed = 0
        for recording in rows:
            record_removed, record_freed = _evict_local_files(recording, dry_run=dry_run)
            removed += record_removed
            freed += record_freed
            if not dry_run:
                recording.storage_state = "evicted"
        if not dry_run:
            await db.commit()

    if removed and not dry_run:
        logging.info("Evicted %s local file(s), freed %s bytes", removed, freed)
    return {"removed": removed, "freed_bytes": freed}


async def evict_local_files(recording_id: str, *, force: bool = False) -> dict:
    """Evict one recording's local files (used by storage-backfill)."""
    async with AsyncSessionLocal() as db:
        recording = (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one_or_none()
        if not recording:
            raise ValueError(f"Recording {recording_id} not found")
        if not force and (recording.archive_verified_at is None or recording.keep_local):
            return {"recording_id": recording_id, "removed": 0, "freed_bytes": 0}

        removed, freed = _evict_local_files(recording)
        recording.storage_state = "evicted"
        await db.commit()
    return {"recording_id": recording_id, "removed": removed, "freed_bytes": freed}
