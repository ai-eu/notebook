import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path


def get_file_recorded_at(path: Path) -> datetime:
    stat = path.stat()
    # Use mtime; for recorder files this is usually the recording time.
    # If the file was copied without preserving mtime, fall back to ctime or now.
    ts = stat.st_mtime or stat.st_ctime
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def generate_recording_id(recorded_at: datetime) -> str:
    date_part = recorded_at.strftime("%Y-%m-%d_%H-%M-%S")
    random_part = secrets.token_urlsafe(6)
    return f"{date_part}_{random_part}"


def user_data_dir(user_id: int) -> Path:
    from app.config import settings
    base = settings.data_dir_absolute
    return base / str(user_id)


def resolve_recording_path(folder_path: str | Path) -> Path:
    """Resolves the absolute path to a recording folder.

    We store the path in the database relative to DATA_DIR (e.g. "2/2026-...")
    so the database and data/ folders can be moved between servers.
    """
    from app.config import settings

    folder = Path(folder_path)
    if folder.is_absolute():
        return folder
    return settings.data_dir_absolute / folder


def safe_delete(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
