import asyncio
import json
import logging
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Recording, User
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
from app.services.denoiser import denoise_to_mp3
from app.services.groq import GroqAuthError, resolve_groq_key
from app.services.formatter import format_transcript
from app.services.storage import telegram
from app.services.storage.archive import archive_recording
from app.services.transcriber import transcribe_file, transcribe_chunks
from app.services.tts import (
    TTSAuthError,
    chunk_text,
    concat_chunks,
    get_provider,
    get_tts_progress,
    clean_text_for_tts,
    normalize_text,
    safe_unlink,
    set_tts_progress,
    split_voice,
    synthesize_chunk,
    tts_progress,
)
from app.utils import resolve_recording_path, safe_delete


async def _archive_to_telegram(recording_id: str) -> None:
    """Copy the finished recording into the channel.

    Runs after the transcript is saved, so the user sees the result immediately and a
    failed upload never turns a good transcription into an error.
    """
    try:
        result = await archive_recording(recording_id)
        logging.info("Archived %s: %s file(s) uploaded", recording_id, result["uploaded"])
    except Exception as exc:
        logging.warning("Archiving %s failed: %s", recording_id, exc)


# In-memory progress of running transcription jobs, mirroring tts_progress:
# recording_id -> {"stage": denoising|converting|transcribing|formatting,
#                  "started_at": iso timestamp, "chunks_done", "chunks_total"}
processing_progress: dict[str, dict] = {}


def set_processing_stage(recording_id: str, stage: str, **extra) -> None:
    processing_progress[recording_id] = {
        "stage": stage,
        "started_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def get_processing_progress(recording_id: str) -> dict:
    return dict(processing_progress.get(recording_id, {}))


def _clear_processing_progress(recording_id: str) -> None:
    processing_progress.pop(recording_id, None)


async def process_recording(recording_id: str):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        recording = result.scalar_one_or_none()
        if not recording:
            return

        # The key belongs to the uploader: transcription runs on their quota.
        user = await db.get(User, recording.user_id)
        api_key = resolve_groq_key(user)

        recording.status = "processing"
        recording.updated_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            folder = resolve_recording_path(recording.folder_path)
            original_path = folder / "original.tmp"
            audio_path = folder / "audio.mp3"
            transcript_path = folder / "transcript.json"
            formatted_path = folder / "formatted.txt"

            if not original_path.exists():
                raise ValueError("Original file not found")

            info = await get_media_info(original_path)
            if not has_audio_stream(info):
                raise ValueError("No audio stream in uploaded file")

            passthrough = is_mp3_passthrough(info)

            if settings.denoise_level in ("light", "deep") and not settings.mock_transcription:
                set_processing_stage(recording_id, "denoising")
                try:
                    await denoise_to_mp3(original_path, audio_path)
                    # The audio was decoded and re-encoded, so passthrough no longer applies.
                    passthrough = False
                except Exception as exc:
                    logging.warning("Denoising failed for %s, using the original audio: %s", recording_id, exc)
                    set_processing_stage(recording_id, "converting")
                    await convert_to_mp3(original_path, audio_path, passthrough=passthrough)
            else:
                set_processing_stage(recording_id, "converting")
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
                transcript = await transcribe_file(audio_path, api_key)
            else:
                def report_chunk_progress(done: int, total: int) -> None:
                    set_processing_stage(recording_id, "transcribing", chunks_done=done, chunks_total=total)

                set_processing_stage(recording_id, "transcribing", chunks_done=0, chunks_total=len(chunk_paths))
                transcript = await transcribe_chunks(chunk_paths, api_key, progress_cb=report_chunk_progress)

            set_processing_stage(recording_id, "formatting")

            transcript_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
            formatted = await format_transcript(
                transcript,
                api_key=api_key,
                use_groq=settings.smart_format,
            )
            formatted_path.write_text(formatted, encoding="utf-8")
            if settings.smart_format:
                (folder / "formatted-smart.txt").write_text(formatted, encoding="utf-8")

            recording.status = "done"
            recording.duration = duration
            recording.updated_at = datetime.now(timezone.utc)
            await db.commit()

        except GroqAuthError as exc:
            logging.warning("Groq rejected the API key of user %s: %s", recording.user_id, exc)
            if user:
                user.key_valid = False
            recording.status = "error"
            recording.error_message = "Groq rejected the API key. Sign in again with a valid key."
            recording.updated_at = datetime.now(timezone.utc)
            await db.commit()
            # Keep files for diagnostics.
        except Exception as exc:
            logging.exception("process_recording failed for %s", recording_id)
            recording.status = "error"
            recording.error_message = f"{type(exc).__name__}: {exc}"
            recording.updated_at = datetime.now(timezone.utc)
            await db.commit()
            # Keep files for diagnostics.

        _clear_processing_progress(recording_id)

        if recording.status == "done" and telegram.is_configured():
            await _archive_to_telegram(recording_id)


async def process_tts(recording_id: str, voice: str | None = None):
    """Turn the uploaded text (original.tmp) into one spoken MP3.

    The result reuses the transcription machinery: audio.mp3, a synthetic
    transcript.json (one segment per chunk, cumulative timings) and formatted.txt,
    so cards, player highlighting and the Telegram archive work unchanged.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        recording = result.scalar_one_or_none()
        if not recording:
            return

        provider_name, voice_name = split_voice(voice or settings.tts_voice)
        recording.tts_model = provider_name
        recording.tts_voice = voice_name
        recording.status = "processing"
        recording.updated_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            provider = get_provider(provider_name)
            folder = resolve_recording_path(recording.folder_path)
            original_path = folder / "original.tmp"
            audio_path = folder / "audio.mp3"
            transcript_path = folder / "transcript.json"
            formatted_path = folder / "formatted.txt"

            if not original_path.exists():
                raise ValueError("Text file not found")

            raw = original_path.read_text(encoding="utf-8", errors="replace")
            text = normalize_text(raw)
            text = clean_text_for_tts(text)
            if not text.strip():
                raise ValueError("Empty text file")
            if len(text) > settings.tts_max_chars:
                raise ValueError(
                    f"Text too long: {len(text)} chars > {settings.tts_max_chars}"
                )

            chunks = chunk_text(text, settings.tts_chunk_chars)
            chunks_dir = folder / "chunks"
            chunks_dir.mkdir(parents=True, exist_ok=True)

            set_tts_progress(recording_id, 0, len(chunks))
            semaphore = asyncio.Semaphore(max(1, settings.tts_concurrency))

            async def synth(index: int) -> Path:
                async with semaphore:
                    out_path = chunks_dir / f"tts-{index:03d}.{provider.audio_format}"
                    await synthesize_chunk(provider, chunks[index], voice_name, out_path)
                    done = get_tts_progress(recording_id).get("chunks_done", 0)
                    set_tts_progress(recording_id, done + 1, len(chunks))
                    return out_path

            chunk_paths = list(await asyncio.gather(*(synth(i) for i in range(len(chunks)))))

            await concat_chunks(chunk_paths, audio_path)
            durations = [await get_duration(path) for path in chunk_paths]
            total_duration = sum(durations)

            segments = []
            offset = 0.0
            for index, (chunk, duration) in enumerate(zip(chunks, durations)):
                segments.append(
                    {
                        "id": index,
                        "start": round(offset, 3),
                        "end": round(offset + duration, 3),
                        "text": chunk,
                    }
                )
                offset += duration

            transcript_path.write_text(
                json.dumps(
                    {
                        "text": text,
                        "language": None,
                        "duration": total_duration,
                        "segments": segments,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            formatted_path.write_text(text, encoding="utf-8")

            safe_delete(original_path)
            safe_delete(chunks_dir)

            recording.status = "done"
            recording.duration = total_duration
            recording.updated_at = datetime.now(timezone.utc)
            await db.commit()

        except TTSAuthError as exc:
            logging.warning("TTS provider rejected the key for %s: %s", recording_id, exc)
            recording.status = "error"
            recording.error_message = "TTS provider rejected the API key. Check the server configuration."
            recording.updated_at = datetime.now(timezone.utc)
            await db.commit()
            # Keep files for diagnostics.
        except Exception as exc:
            logging.exception("process_tts failed for %s", recording_id)
            recording.status = "error"
            recording.error_message = f"{type(exc).__name__}: {exc}"
            recording.updated_at = datetime.now(timezone.utc)
            await db.commit()
            # Keep files for diagnostics.

        if recording.status == "done" and telegram.is_configured():
            await _archive_to_telegram(recording_id)

        tts_progress.pop(recording_id, None)
