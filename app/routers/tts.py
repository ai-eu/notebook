import asyncio
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_user
from app.config import settings
from app.database import get_db
from app.models import Recording, User
from app.services.processing import process_tts
from app.services.tts import curated_voices
from app.utils import generate_recording_id, user_data_dir

router = APIRouter(prefix="/api/tts", tags=["tts"])

# Keep references to running tasks so they are not garbage-collected.
_running_tasks = set()

TEXT_EXTENSIONS = {".txt", ".md"}


def _start_tts(recording_id: str, voice: str | None) -> None:
    task = asyncio.create_task(process_tts(recording_id, voice=voice))
    _running_tasks.add(task)

    def _on_done(t):
        _running_tasks.discard(t)
        try:
            t.result()
        except Exception:
            pass

    task.add_done_callback(_on_done)


@router.get("/voices")
async def tts_voices(user: User = Depends(require_user)):
    return {
        "provider": settings.tts_provider,
        "enabled": settings.tts_enabled,
        "default_voice": settings.tts_voice,
        "max_chars": settings.tts_max_chars,
        "max_file_mb": settings.tts_max_file_mb,
        "voices": curated_voices(),
    }


@router.post("/upload")
async def tts_upload(
    file: UploadFile = File(...),
    voice: str = Form(""),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if not settings.tts_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="TTS is disabled")

    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No filename")

    if Path(file.filename).suffix.lower() not in TEXT_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Please upload a .txt or .md file.",
        )

    max_bytes = settings.tts_max_file_mb * 1024 * 1024
    data = await file.read()
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File too large: {settings.tts_max_file_mb} MB limit for text files.",
        )

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")
    if not text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty text file")
    if len(text) > settings.tts_max_chars:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Text too long: {len(text)} characters, limit is {settings.tts_max_chars}.",
        )

    recording_id = generate_recording_id(datetime.now(timezone.utc))
    folder = user_data_dir(user.id) / recording_id
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "original.tmp").write_text(text, encoding="utf-8")

    relative_folder = folder.relative_to(settings.data_dir_absolute).as_posix()
    recording = Recording(
        user_id=user.id,
        recording_id=recording_id,
        folder_path=relative_folder,
        original_filename=file.filename,
        status="pending",
        kind="tts",
    )
    db.add(recording)
    await db.commit()

    _start_tts(recording_id, voice.strip() or None)

    return {
        "recording_id": recording_id,
        "status": recording.status,
        "original_filename": file.filename,
    }
