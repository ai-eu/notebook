import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import require_user
from app.models import Recording, RecordingArtifact, User
from app.services.formatter import MAX_LINE_LENGTH, render_formatted
from app.services.processing import get_processing_progress
from app.services.tts import get_tts_progress
from app.services.storage.archive import (
    enqueue_remote_cleanup,
    process_remote_cleanup,
)
from app.services.storage.artifacts import (
    KIND_FORMATTED,
    ensure_artifacts,
    load_sentences,
    load_transcript,
    save_formatted,
)
from app.services.storage.cache import drop as drop_cached
from app.services.storage.cache import ensure_cached
from app.utils import resolve_recording_path, safe_delete

router = APIRouter(prefix="/api", tags=["recordings"])


def _recording_to_dict(
    recording: Recording,
    *,
    txt_ready: bool,
    include_transcript: bool = False,
    transcript: dict | None = None,
) -> dict:
    data = {
        "recording_id": recording.recording_id,
        "original_filename": recording.original_filename,
        "status": recording.status,
        "created_at": recording.created_at.isoformat() if recording.created_at else None,
        "updated_at": recording.updated_at.isoformat() if recording.updated_at else None,
        "duration": recording.duration,
        "error_message": recording.error_message,
        "tags": recording.tags or "",
        "comment": recording.comment,
        "txt_ready": txt_ready,
    }
    if include_transcript:
        data["transcript"] = transcript
    return data


async def _formatted_recording_ids(
    db: AsyncSession, recording_ids: list[str]
) -> set[str]:
    if not recording_ids:
        return set()
    rows = await db.execute(
        select(RecordingArtifact.recording_id).where(
            RecordingArtifact.kind == KIND_FORMATTED,
            RecordingArtifact.recording_id.in_(recording_ids),
        )
    )
    return set(rows.scalars())


async def _has_formatted(db: AsyncSession, recording_id: str) -> bool:
    return (
        await db.scalar(
            select(RecordingArtifact.id).where(
                RecordingArtifact.recording_id == recording_id,
                RecordingArtifact.kind == KIND_FORMATTED,
            )
        )
        is not None
    )


async def _get_user_recording(recording_id: str, user: User, db: AsyncSession) -> Recording:
    result = await db.execute(
        select(Recording).where(
            Recording.recording_id == recording_id,
            Recording.user_id == user.id,
        )
    )
    recording = result.scalar_one_or_none()
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")
    return recording


@router.get("/recordings")
async def list_recordings(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Recording)
        .where(Recording.user_id == user.id)
        .order_by(Recording.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    recordings = result.scalars().all()
    formatted_ids = await _formatted_recording_ids(
        db, [recording.recording_id for recording in recordings]
    )
    return [
        _recording_to_dict(
            recording,
            txt_ready=recording.recording_id in formatted_ids,
        )
        for recording in recordings
    ]


@router.get("/recordings/{recording_id}")
async def get_recording(
    recording_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    recording = await _get_user_recording(recording_id, user, db)
    artifacts = await ensure_artifacts(db, recording)
    return _recording_to_dict(
        recording,
        txt_ready=KIND_FORMATTED in artifacts,
        include_transcript=True,
        transcript=await load_transcript(db, recording.recording_id),
    )


@router.get("/recordings/{recording_id}/status")
async def get_recording_status(
    recording_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    recording = await _get_user_recording(recording_id, user, db)
    legacy_formatted = resolve_recording_path(recording.folder_path) / "formatted.txt"
    result = {
        "recording_id": recording.recording_id,
        "status": recording.status,
        "duration": recording.duration,
        "error_message": recording.error_message,
        "txt_ready": await _has_formatted(db, recording.recording_id) or legacy_formatted.exists(),
    }
    if recording.kind == "tts":
        result["kind"] = "tts"
        result["tts_model"] = recording.tts_model
        result["tts_voice"] = recording.tts_voice
        result["tts_progress"] = get_tts_progress(recording_id)
    else:
        result["progress"] = get_processing_progress(recording_id)
    return result


class TagsUpdate(BaseModel):
    tags: str = ""
    comment: str = ""


def _normalize_tags(raw: str) -> str:
    # Split on any whitespace, drop duplicates keeping the first occurrence
    tags = [tag[:50] for tag in dict.fromkeys(raw.split())]
    return " ".join(tags[:20])


@router.put("/recordings/{recording_id}/tags")
async def update_recording_tags(
    recording_id: str,
    payload: TagsUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    recording = await _get_user_recording(recording_id, user, db)
    recording.tags = _normalize_tags(payload.tags) or None
    recording.comment = payload.comment.strip() or None
    await db.commit()
    return _recording_to_dict(
        recording,
        txt_ready=await _has_formatted(db, recording.recording_id),
    )


@router.get("/recordings/{recording_id}/audio")
async def get_recording_audio(
    recording_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    recording = await _get_user_recording(recording_id, user, db)
    audio_path = resolve_recording_path(recording.folder_path) / "audio.mp3"
    if not audio_path.exists():
        # The audio was moved into the channel; pull it back on the first listen.
        audio_path = await ensure_cached(recording_id)
    if not audio_path or not audio_path.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The audio is in the archive and could not be downloaded right now. Please try again.",
        )
    return FileResponse(audio_path, media_type="audio/mpeg", filename="audio.mp3")


@router.delete("/recordings/{recording_id}")
async def delete_recording(
    recording_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    recording = await _get_user_recording(recording_id, user, db)
    # Queue channel messages before the StoredFile rows disappear with the recording.
    await enqueue_remote_cleanup(recording_id)
    drop_cached(recording_id)
    safe_delete(resolve_recording_path(recording.folder_path))
    await db.delete(recording)
    await db.commit()
    await process_remote_cleanup()
    return {"deleted": True}


def _txt_filename(recording: Recording) -> str:
    base = recording.original_filename or recording.recording_id
    base = re.sub(r'[\\/:*?"<>|]', "", base)
    stem = Path(base).stem or recording.recording_id
    return f"{stem}.txt"


@router.get("/recordings/{recording_id}/download")
async def download_recording(
    recording_id: str,
    smart: bool = Query(False, description="Deprecated: formatting is precomputed"),
    refresh: bool = Query(False, description="Re-render the text from the saved sentences"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    _ = smart
    recording = await _get_user_recording(recording_id, user, db)
    artifacts = await ensure_artifacts(db, recording)

    if refresh:
        document = await load_sentences(db, recording.recording_id)
        if document:
            artifacts[KIND_FORMATTED] = render_formatted(
                document, max_line_length=MAX_LINE_LENGTH
            )
            await save_formatted(
                db, recording.recording_id, artifacts[KIND_FORMATTED]
            )

    formatted = artifacts.get(KIND_FORMATTED)
    if formatted is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    filename = _txt_filename(recording)

    return Response(
        content=formatted.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
