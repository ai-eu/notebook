import json
import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse, Response
from app.config import settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import require_user
from app.models import User, Recording
from app.services.formatter import format_transcript
from app.services.groq import resolve_groq_key
from app.services.storage.archive import delete_remote_copy
from app.services.storage.cache import drop as drop_cached
from app.services.storage.cache import ensure_cached
from app.utils import resolve_recording_path, safe_delete

router = APIRouter(prefix="/api", tags=["recordings"])


def _recording_to_dict(recording: Recording, include_transcript: bool = False) -> dict:
    folder = resolve_recording_path(recording.folder_path)
    data = {
        "recording_id": recording.recording_id,
        "original_filename": recording.original_filename,
        "status": recording.status,
        "created_at": recording.created_at.isoformat() if recording.created_at else None,
        "updated_at": recording.updated_at.isoformat() if recording.updated_at else None,
        "duration": recording.duration,
        "error_message": recording.error_message,
        "txt_ready": (folder / "formatted.txt").exists(),
    }
    if include_transcript:
        transcript_path = folder / "transcript.json"
        if transcript_path.exists():
            data["transcript"] = json.loads(transcript_path.read_text(encoding="utf-8"))
        else:
            data["transcript"] = None
    return data


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
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Recording)
        .where(Recording.user_id == user.id)
        .order_by(Recording.created_at.desc())
    )
    recordings = result.scalars().all()
    return [_recording_to_dict(r) for r in recordings]


@router.get("/recordings/{recording_id}")
async def get_recording(
    recording_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    recording = await _get_user_recording(recording_id, user, db)
    return _recording_to_dict(recording, include_transcript=True)


@router.get("/recordings/{recording_id}/status")
async def get_recording_status(
    recording_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    recording = await _get_user_recording(recording_id, user, db)
    formatted_path = resolve_recording_path(recording.folder_path) / "formatted.txt"
    return {
        "recording_id": recording.recording_id,
        "status": recording.status,
        "duration": recording.duration,
        "error_message": recording.error_message,
        "txt_ready": formatted_path.exists(),
    }


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
    # Drop the copies in the channel and in the cache before the row goes away.
    await delete_remote_copy(recording_id)
    drop_cached(recording_id)
    safe_delete(resolve_recording_path(recording.folder_path))
    await db.delete(recording)
    await db.commit()
    return {"deleted": True}


def _txt_filename(recording: Recording) -> str:
    base = recording.original_filename or recording.recording_id
    base = re.sub(r'[\\/:*?"<>|]', "", base)
    stem = Path(base).stem or recording.recording_id
    return f"{stem}.txt"


@router.get("/recordings/{recording_id}/download")
async def download_recording(
    recording_id: str,
    smart: bool = Query(False, description="Use Groq to split long lines"),
    refresh: bool = Query(False, description="Recreate the cache, ignoring the saved formatted text"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    recording = await _get_user_recording(recording_id, user, db)
    transcript_path = resolve_recording_path(recording.folder_path) / "transcript.json"
    if not transcript_path.exists():
        raise HTTPException(status_code=404, detail="Transcript not found")

    use_groq = smart or settings.smart_format
    cache_name = "formatted-smart.txt" if use_groq else "formatted.txt"
    cache_path = resolve_recording_path(recording.folder_path) / cache_name

    formatted = None
    if not refresh and cache_path.exists():
        try:
            transcript_mtime = transcript_path.stat().st_mtime
            cache_mtime = cache_path.stat().st_mtime
            if cache_mtime >= transcript_mtime:
                formatted = cache_path.read_text(encoding="utf-8")
        except OSError:
            pass

    if formatted is None:
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        formatted = await format_transcript(
            transcript,
            api_key=resolve_groq_key(user),
            use_groq=use_groq,
        )
        try:
            cache_path.write_text(formatted, encoding="utf-8")
        except OSError:
            pass

    filename = _txt_filename(recording)

    return Response(
        content=formatted.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
