import asyncio
import logging
import mimetypes
from pathlib import Path
from app.config import settings
from app.models import User
from app.services.converter import get_duration
from app.services.formatter import clean_transcript
from app.services.groq import (
    GROQ_API_BASE,
    GroqAuthError,
    TranscriptionAuthError,
    resolve_groq_key,
)
from app.services.http import post_with_retries


GROQ_TRANSCRIPTION_URL = f"{GROQ_API_BASE}/audio/transcriptions"
OPENROUTER_TRANSCRIPTION_PATH = "/audio/transcriptions"
# Both providers cap one upload at 25 MB; longer audio is split into chunks.
MAX_UPLOAD_MB = 25.0
# Words separated by a longer pause open a new segment when a provider returns
# word timestamps but no segments.
SEGMENT_GAP_SECONDS = 0.8

MOCK_TEXTS = [
    "This is a test recording.",
    "We are checking the transcription feature.",
    "The second sentence appears here.",
    "The third part of the mock transcript.",
    "Final sentence for testing highlights.",
]


async def _mock_transcribe(mp3_path: Path) -> dict:
    duration = await get_duration(mp3_path)
    segment_count = min(len(MOCK_TEXTS), max(1, int(duration / 1.5)))
    chunk = duration / segment_count
    segments = []
    for i in range(segment_count):
        text = MOCK_TEXTS[i % len(MOCK_TEXTS)]
        start = round(i * chunk, 3)
        end = round((i + 1) * chunk, 3)
        segments.append({"id": i, "start": start, "end": end, "text": text})
    return {
        "text": " ".join([s["text"] for s in segments]),
        "language": "en",
        "duration": duration,
        "segments": segments,
    }


def transcription_provider() -> str:
    """Active provider: ``groq`` (default) or ``openrouter``."""
    return (settings.transcription_provider or "groq").strip().lower()


def resolve_transcription_key(user: User | None = None) -> str | None:
    """The key for the active provider.

    Groq bills the uploader's own key; OpenRouter runs on the shared server key,
    so a user without a Groq key still gets a transcript.
    """
    if transcription_provider() == "openrouter":
        return settings.openrouter_api_key
    return resolve_groq_key(user)


def _audio_mime(mp3_path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(mp3_path))
    return mime or "audio/mpeg"


def _error_detail(response) -> str:
    return str(getattr(response, "text", "") or "")[:500]


async def transcribe_file(mp3_path: Path, api_key: str | None, language: str | None = None) -> dict:
    """Transcribe one audio file with the configured provider.

    ``api_key`` is that provider's key; ``resolve_transcription_key`` picks it.
    """
    file_size_mb = mp3_path.stat().st_size / (1024 * 1024)
    if file_size_mb > MAX_UPLOAD_MB:
        raise ValueError(
            f"File too large for transcription: {file_size_mb:.2f} MB > {MAX_UPLOAD_MB:.0f} MB"
        )

    if settings.mock_transcription:
        return await _mock_transcribe(mp3_path)

    if transcription_provider() == "openrouter":
        transcript = await _transcribe_with_openrouter(mp3_path, api_key, language)
    else:
        transcript = await _transcribe_with_groq(mp3_path, api_key, language)
    return clean_transcript(transcript)


async def _transcribe_with_groq(
    mp3_path: Path, api_key: str | None, language: str | None = None
) -> dict:
    if not api_key:
        raise ValueError("No Groq API key available for this user")

    data = {
        "model": settings.whisper_model,
        "response_format": "verbose_json",
    }
    if settings.whisper_prompt:
        data["prompt"] = settings.whisper_prompt
    if language:
        data["language"] = language

    with open(mp3_path, "rb") as f:
        files = {"file": (mp3_path.name, f, _audio_mime(mp3_path))}
        headers = {"Authorization": f"Bearer {api_key}"}
        response = await post_with_retries(
            GROQ_TRANSCRIPTION_URL,
            data=data,
            files=files,
            headers=headers,
            timeout=300.0,
        )

    if response.status_code in (401, 403):
        raise GroqAuthError(f"Groq rejected the API key (HTTP {response.status_code})")

    if response.status_code != 200:
        raise ValueError(f"Groq API error {response.status_code}: {_error_detail(response)}")

    return response.json()


async def _transcribe_with_openrouter(
    mp3_path: Path, api_key: str | None, language: str | None = None
) -> dict:
    """Transcribe through OpenRouter's OpenAI-compatible STT endpoint.

    OpenRouter has no ``prompt``: vocabulary is biased with ``keyterms`` instead,
    and sentence timings come from ``verbose_json`` segments (or words).
    """
    if not api_key:
        raise ValueError("No OpenRouter API key configured for transcription")

    url = f"{settings.openrouter_base_url.rstrip('/')}{OPENROUTER_TRANSCRIPTION_PATH}"
    data: dict[str, object] = {
        "model": settings.openrouter_transcription_model,
        "response_format": "verbose_json",
        "timestamp_granularities[]": ["segment", "word"],
    }
    if language:
        data["language"] = language
    if settings.openrouter_keyterms:
        data["keyterms[]"] = list(settings.openrouter_keyterms)

    with open(mp3_path, "rb") as f:
        files = {"file": (mp3_path.name, f, _audio_mime(mp3_path))}
        headers = {"Authorization": f"Bearer {api_key}"}
        response = await post_with_retries(
            url,
            data=data,
            files=files,
            headers=headers,
            timeout=600.0,
        )

    if response.status_code in (401, 403):
        raise TranscriptionAuthError(
            f"OpenRouter rejected the API key (HTTP {response.status_code})"
        )

    if response.status_code == 402:
        raise ValueError(
            f"OpenRouter has not enough credits (HTTP 402): {_error_detail(response)}"
        )

    if response.status_code != 200:
        raise ValueError(
            f"OpenRouter API error {response.status_code}: {_error_detail(response)}"
        )

    payload = response.json()
    usage = payload.get("usage") or {}
    logging.info(
        "OpenRouter %s: %.1fs of audio, cost $%s",
        settings.openrouter_transcription_model,
        float(usage.get("seconds") or 0.0),
        usage.get("cost"),
    )
    return await _normalize_openrouter_result(payload, mp3_path)


def _segments_from_words(words: list[dict]) -> list[dict]:
    """Group word timestamps into sentence-like segments.

    OpenRouter passes word timestamps through even from providers that return no
    segments, so per-sentence playback keeps working.
    """
    segments: list[dict] = []
    buffer: list[str] = []
    start = 0.0
    end = 0.0

    def flush() -> None:
        if buffer:
            segments.append(
                {"id": len(segments), "start": start, "end": end, "text": " ".join(buffer)}
            )
            buffer.clear()

    for word in words:
        text = " ".join(str(word.get("word") or word.get("text") or "").split())
        if not text:
            continue
        word_start = float(word.get("start") or 0.0)
        word_end = max(word_start, float(word.get("end") or word_start))
        if buffer and word_start - end > SEGMENT_GAP_SECONDS:
            flush()
        if not buffer:
            start = word_start
        buffer.append(text)
        end = word_end
        if text[-1] in ".!?…":
            flush()
    flush()
    return segments


async def _normalize_openrouter_result(payload: dict, mp3_path: Path) -> dict:
    """Bring an OpenRouter transcript to the Whisper ``verbose_json`` shape.

    Chunks are offset by ``duration`` and sentences are aligned by segment
    timings, so both are filled in even when the provider omits them.
    """
    raw_segments = payload.get("segments") or []
    if not raw_segments:
        raw_segments = _segments_from_words(payload.get("words") or [])

    segments: list[dict] = []
    for index, segment in enumerate(raw_segments):
        text = " ".join(str(segment.get("text") or "").split())
        if not text:
            continue
        start = float(segment.get("start") or 0.0)
        end = max(start, float(segment.get("end") or start))
        segments.append(
            {"id": segment.get("id", index), "start": start, "end": end, "text": text}
        )

    duration = payload.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = segments[-1]["end"] if segments else await get_duration(mp3_path)

    text = " ".join(str(payload.get("text") or "").split())
    return {
        "text": text or " ".join(segment["text"] for segment in segments),
        "language": payload.get("language"),
        "duration": float(duration),
        "segments": segments,
    }


async def transcribe_chunks(
    chunk_paths: list[Path],
    api_key: str | None,
    language: str | None = None,
    progress_cb=None,
) -> dict:
    results: list[dict | None] = [None] * len(chunk_paths)
    semaphore = asyncio.Semaphore(max(1, settings.transcription_concurrency))
    progress_lock = asyncio.Lock()
    done = 0

    async def worker(index: int, chunk_path: Path) -> None:
        nonlocal done
        async with semaphore:
            results[index] = await transcribe_file(chunk_path, api_key, language=language)
        if progress_cb:
            async with progress_lock:
                done += 1
                progress_cb(done, len(chunk_paths))

    await asyncio.gather(
        *(worker(index, path) for index, path in enumerate(chunk_paths))
    )

    all_segments = []
    full_texts = []
    detected_language = None
    duration = 0.0
    offset = 0.0

    for result in results:
        if result is None:
            raise ValueError("Transcription chunk returned no result")
        chunk_duration = result.get("duration", 0.0)
        for segment in result.get("segments", []):
            shifted = dict(segment)
            shifted["start"] = segment["start"] + offset
            shifted["end"] = segment["end"] + offset
            all_segments.append(shifted)
        full_texts.append(result.get("text", "").strip())
        if "language" in result and not detected_language:
            detected_language = result["language"]
        offset += chunk_duration
        duration += chunk_duration

    return {
        "text": " ".join(full_texts),
        "language": detected_language,
        "duration": duration,
        "segments": all_segments,
    }
