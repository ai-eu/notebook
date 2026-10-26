import asyncio
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, UploadFile, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import require_user
from app.models import User, Recording
from app.config import settings
from app.utils import (
    generate_recording_id,
    get_file_recorded_at,
    safe_delete,
    user_data_dir,
)
from app.services.jobs import job_runner
from app.services.processing import process_recording

router = APIRouter(prefix="/api", tags=["upload"])


def _start_processing(recording_id: str) -> None:
    job_runner.submit(lambda: process_recording(recording_id))


ALLOWED_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".wma", ".aac", ".amr", ".3gp", ".webm", ".mp4", ".mov", ".mkv", ".caf", ".aif", ".aiff"}


def _has_audio_extension(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_EXTENSIONS


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    last_modified: int = Form(0),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No filename")

    if not _has_audio_extension(file.filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Please upload an audio or video file.",
        )

    max_bytes = settings.upload_max_size_bytes
    announced_size = getattr(file, "size", None)
    if announced_size is not None and announced_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large: {settings.upload_max_size_mb} MB limit.",
        )

    tmp_path: Path | None = None
    folder: Path | None = None
    try:
        # Save to a temporary file to determine mtime, enforcing the size limit
        # while copying so an oversized upload is rejected before processing.
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
            tmp_path = Path(tmp.name)
            written = 0
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large: {settings.upload_max_size_mb} MB limit.",
                    )
                await asyncio.to_thread(tmp.write, chunk)

        if last_modified:
            recorded_at = datetime.fromtimestamp(last_modified / 1000.0, tz=timezone.utc)
        else:
            recorded_at = get_file_recorded_at(tmp_path)
        recording_id = generate_recording_id(recorded_at)
        candidate = user_data_dir(user.id) / recording_id
        candidate.mkdir(parents=True, exist_ok=False)
        folder = candidate

        original_path = folder / "original.tmp"
        await asyncio.to_thread(shutil.move, str(tmp_path), str(original_path))

        relative_folder = folder.relative_to(settings.data_dir_absolute).as_posix()
        recording = Recording(
            user_id=user.id,
            recording_id=recording_id,
            folder_path=relative_folder,
            original_filename=file.filename,
            status="pending",
        )
        db.add(recording)
        await db.commit()

        _start_processing(recording_id)

        return {
            "recording_id": recording_id,
            "status": recording.status,
            "original_filename": file.filename,
        }
    except HTTPException:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
        if folder is not None:
            safe_delete(folder)
        raise
    except Exception as exc:
        # Clean up the temporary file and the folder on error.
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
        if folder is not None:
            safe_delete(folder)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
