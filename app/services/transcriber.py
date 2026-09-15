import json
import mimetypes
from pathlib import Path
import httpx
from app.config import settings
from app.services.converter import get_duration


GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

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


async def transcribe_file(mp3_path: Path, language: str | None = None) -> dict:
    file_size_mb = mp3_path.stat().st_size / (1024 * 1024)
    if file_size_mb > 25:
        raise ValueError(f"File too large for Groq: {file_size_mb:.2f} MB > 25 MB")

    if settings.mock_transcription:
        return await _mock_transcribe(mp3_path)

    mime, _ = mimetypes.guess_type(str(mp3_path))
    if not mime:
        mime = "audio/mpeg"

    data = {
        "model": settings.whisper_model,
        "response_format": "verbose_json",
    }
    if settings.whisper_prompt:
        data["prompt"] = settings.whisper_prompt
    if language:
        data["language"] = language

    async with httpx.AsyncClient(timeout=300.0) as client:
        with open(mp3_path, "rb") as f:
            files = {"file": (mp3_path.name, f, mime)}
            headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
            response = await client.post(
                GROQ_TRANSCRIPTION_URL,
                data=data,
                files=files,
                headers=headers,
            )

    if response.status_code != 200:
        raise ValueError(f"Groq API error {response.status_code}: {response.text}")

    return response.json()


async def transcribe_chunks(chunk_paths: list[Path], language: str | None = None) -> dict:
    all_segments = []
    full_texts = []
    detected_language = None
    duration = 0.0
    offset = 0.0

    for chunk_path in chunk_paths:
        result = await transcribe_file(chunk_path, language=language)
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
