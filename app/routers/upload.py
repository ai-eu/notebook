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
from app.utils import user_data_dir, get_file_recorded_at, generate_recording_id
from app.services.processing import process_recording

router = APIRouter(prefix="/api", tags=["upload"])

# Keep references to running tasks so they are not garbage-collected.
_running_tasks = set()


def _start_processing(recording_id: str) -> None:
    task = asyncio.create_task(process_recording(recording_id))
    _running_tasks.add(task)

    def _on_done(t):
        _running_tasks.discard(t)
        try:
            t.result()
        except Exception:
            pass

    task.add_done_callback(_on_done)


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

    # Save to a temporary file to determine mtime.
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
        await asyncio.to_thread(shutil.copyfileobj, file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        if last_modified:
            recorded_at = datetime.fromtimestamp(last_modified / 1000.0, tz=timezone.utc)
        else:
            recorded_at = get_file_recorded_at(tmp_path)
        recording_id = generate_recording_id(recorded_at)
        folder = user_data_dir(user.id) / recording_id
        folder.mkdir(parents=True, exist_ok=False)

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
    except Exception as exc:
        # Clean up the temporary file and folder on error.
        if tmp_path.exists():
            tmp_path.unlink()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
