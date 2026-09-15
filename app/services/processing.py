import asyncio
import json
import logging
import shutil
import traceback
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Recording
from app.config import settings
from app.services.converter import (
    convert_to_mp3,
    get_media_info,
    has_audio_stream,
    get_duration,
    get_file_size_mb,
    split_audio,
    is_mp3_passthrough,
    get_audio_bitrate_kbps,
    calculate_chunk_minutes,
)
from app.services.transcriber import transcribe_file, transcribe_chunks
from app.utils import resolve_recording_path, safe_delete


async def process_recording(recording_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        recording = result.scalar_one_or_none()
        if not recording:
            return

        recording.status = "processing"
        recording.updated_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            folder = resolve_recording_path(recording.folder_path)
            original_path = folder / "original.tmp"
            audio_path = folder / "audio.mp3"
            transcript_path = folder / "transcript.json"

            if not original_path.exists():
                raise ValueError("Original file not found")

            info = await get_media_info(original_path)
            if not has_audio_stream(info):
                raise ValueError("No audio stream in uploaded file")

            passthrough = is_mp3_passthrough(info)

            await convert_to_mp3(original_path, audio_path, passthrough=passthrough)
            duration = await get_duration(audio_path)

            # Remove the original after conversion.
            safe_delete(original_path)

            # Determine the output bitrate for chunk size calculation.
            if passthrough:
                output_bitrate = get_audio_bitrate_kbps(info) or settings.target_mp3_kbps
            else:
                output_bitrate = settings.target_mp3_kbps

            chunk_minutes = calculate_chunk_minutes(
                output_bitrate,
                settings.target_chunk_mb,
                settings.chunk_max_minutes,
            )

            # Check if chunking is needed.
            chunk_paths = [audio_path]
            file_size_mb = get_file_size_mb(audio_path)
            if file_size_mb > settings.target_chunk_mb or duration > chunk_minutes * 60:
                chunks_dir = folder / "chunks"
                chunk_paths = await split_audio(audio_path, chunks_dir, chunk_minutes, passthrough=passthrough)

            if len(chunk_paths) == 1:
                transcript = await transcribe_file(audio_path)
            else:
                transcript = await transcribe_chunks(chunk_paths)

            transcript_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")

            recording.status = "done"
            recording.duration = duration
            recording.updated_at = datetime.now(timezone.utc)
            await db.commit()

        except Exception as exc:
            logging.exception("process_recording failed for %s", recording_id)
            recording.status = "error"
            recording.error_message = f"{type(exc).__name__}: {exc}"
            recording.updated_at = datetime.now(timezone.utc)
            await db.commit()
            # Keep files for diagnostics.
