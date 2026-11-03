import asyncio

import pytest

from app.config import settings
from app.models import User
from app.services import http as http_service, transcriber
from app.services.groq import GroqAuthError, TranscriptionAuthError
from conftest import FakeAsyncClient, FakeResponse

KEY = "gsk_" + "a" * 52
OPENROUTER_KEY = "sk-or-v1-" + "b" * 32


@pytest.fixture
def mp3_file(tmp_path):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"ID3 fake audio")
    return path


def test_sends_the_callers_key_to_groq(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    fake = FakeAsyncClient(FakeResponse(200, {"text": "hello", "segments": []}))
    monkeypatch.setattr(http_service.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(transcriber.transcribe_file(mp3_file, KEY))

    assert result == {"text": "hello", "segments": []}
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert fake.requests[0]["url"].endswith("/audio/transcriptions")


def test_chunks_reuse_the_same_key(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    fake = FakeAsyncClient(FakeResponse(200, {"text": "hi", "segments": [], "duration": 1.0}))
    monkeypatch.setattr(http_service.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(transcriber.transcribe_chunks([mp3_file, mp3_file], KEY))

    assert len(fake.requests) == 2
    assert all(r["headers"]["Authorization"] == f"Bearer {KEY}" for r in fake.requests)
    assert result["text"] == "hi hi"


def test_chunks_respect_the_concurrency_limit(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    monkeypatch.setattr(settings, "transcription_concurrency", 2)
    active = 0
    peak = 0

    async def fake_transcribe(path, api_key, language=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"text": "hi", "segments": [], "duration": 1.0}

    monkeypatch.setattr(transcriber, "transcribe_file", fake_transcribe)

    result = asyncio.run(
        transcriber.transcribe_chunks([mp3_file] * 5, KEY)
    )

    assert peak == 2
    assert result["text"] == "hi hi hi hi hi"


def test_rejected_key_raises_an_auth_error(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    monkeypatch.setattr(
        http_service.httpx,
        "AsyncClient",
        lambda **kwargs: FakeAsyncClient(FakeResponse(401, {"error": "Invalid API Key"})),
    )

    with pytest.raises(GroqAuthError):
        asyncio.run(transcriber.transcribe_file(mp3_file, KEY))


def test_other_errors_stay_plain_value_errors(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    monkeypatch.setattr(
        http_service.httpx,
        "AsyncClient",
        lambda **kwargs: FakeAsyncClient(FakeResponse(500, {"error": "boom"})),
    )

    with pytest.raises(ValueError, match="500"):
        asyncio.run(transcriber.transcribe_file(mp3_file, KEY))


def test_a_missing_key_fails_before_calling_groq(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    monkeypatch.setattr(
        http_service.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("Groq must not be called without a key"),
    )

    with pytest.raises(ValueError, match="No Groq API key"):
        asyncio.run(transcriber.transcribe_file(mp3_file, None))


def test_mock_mode_never_calls_groq(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", True)

    async def fake_duration(path):
        return 3.0

    monkeypatch.setattr(transcriber, "get_duration", fake_duration)
    monkeypatch.setattr(
        http_service.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("Groq must not be called in mock mode"),
    )

    result = asyncio.run(transcriber.transcribe_file(mp3_file, None))

    assert result["text"]
    assert result["duration"] == 3.0


@pytest.fixture
def openrouter(monkeypatch):
    monkeypatch.setattr(settings, "mock_transcription", False)
    monkeypatch.setattr(settings, "transcription_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", OPENROUTER_KEY)
    monkeypatch.setattr(settings, "openrouter_keyterms", [])
    return OPENROUTER_KEY


def test_openrouter_transcribes_through_the_openai_compatible_endpoint(
    monkeypatch, openrouter, mp3_file
):
    fake = FakeAsyncClient(
        FakeResponse(
            200,
            {
                "text": "Hello there. Next one.",
                "language": "en",
                "duration": 4.0,
                "segments": [
                    {"id": 0, "start": 0.0, "end": 1.5, "text": "Hello there."},
                    {"id": 1, "start": 2.0, "end": 4.0, "text": "Next one."},
                ],
                "usage": {"seconds": 4.0, "cost": 0.000132},
            },
        )
    )
    monkeypatch.setattr(http_service.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(transcriber.transcribe_file(mp3_file, OPENROUTER_KEY))

    request = fake.requests[0]
    assert request["url"].endswith("/api/v1/audio/transcriptions")
    assert request["headers"]["Authorization"] == f"Bearer {OPENROUTER_KEY}"
    assert request["data"]["model"] == settings.openrouter_transcription_model
    assert request["data"]["response_format"] == "verbose_json"
    assert request["data"]["timestamp_granularities[]"] == ["segment", "word"]
    assert "prompt" not in request["data"]
    assert result["text"] == "Hello there. Next one."
    assert result["duration"] == 4.0
    assert [segment["text"] for segment in result["segments"]] == ["Hello there.", "Next one."]


def test_openrouter_passes_keyterms_instead_of_a_prompt(monkeypatch, openrouter, mp3_file):
    monkeypatch.setattr(settings, "openrouter_keyterms", ["Nemotron", "Groq"])
    fake = FakeAsyncClient(FakeResponse(200, {"text": "hi", "duration": 1.0}))
    monkeypatch.setattr(http_service.httpx, "AsyncClient", lambda **kwargs: fake)

    asyncio.run(transcriber.transcribe_file(mp3_file, OPENROUTER_KEY))

    assert fake.requests[0]["data"]["keyterms[]"] == ["Nemotron", "Groq"]


def test_openrouter_builds_segments_from_word_timestamps(monkeypatch, openrouter, mp3_file):
    fake = FakeAsyncClient(
        FakeResponse(
            200,
            {
                "text": "One two. Three four.",
                "duration": 5.0,
                "words": [
                    {"word": "One", "start": 0.0, "end": 0.3},
                    {"word": "two.", "start": 0.4, "end": 0.8},
                    {"word": "Three", "start": 3.0, "end": 3.4},
                    {"word": "four.", "start": 3.5, "end": 3.9},
                ],
            },
        )
    )
    monkeypatch.setattr(http_service.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(transcriber.transcribe_file(mp3_file, OPENROUTER_KEY))

    assert [segment["text"] for segment in result["segments"]] == ["One two.", "Three four."]
    assert result["segments"][0]["start"] == 0.0
    assert result["segments"][1]["end"] == 3.9
    assert result["segments"][1]["id"] == 1


def test_openrouter_probes_the_duration_when_the_answer_has_none(
    monkeypatch, openrouter, mp3_file
):
    fake = FakeAsyncClient(FakeResponse(200, {"text": "hi"}))
    monkeypatch.setattr(http_service.httpx, "AsyncClient", lambda **kwargs: fake)

    async def fake_duration(path):
        return 7.5

    monkeypatch.setattr(transcriber, "get_duration", fake_duration)

    result = asyncio.run(transcriber.transcribe_file(mp3_file, OPENROUTER_KEY))

    assert result["duration"] == 7.5
    assert result["segments"] == []
    assert result["text"] == "hi"


def test_openrouter_rejected_key_raises_a_transcription_auth_error(
    monkeypatch, openrouter, mp3_file
):
    monkeypatch.setattr(
        http_service.httpx,
        "AsyncClient",
        lambda **kwargs: FakeAsyncClient(
            FakeResponse(401, {"error": {"code": 401, "message": "No auth credentials found"}})
        ),
    )

    with pytest.raises(TranscriptionAuthError):
        asyncio.run(transcriber.transcribe_file(mp3_file, OPENROUTER_KEY))


def test_openrouter_reports_missing_credits(monkeypatch, openrouter, mp3_file):
    monkeypatch.setattr(
        http_service.httpx,
        "AsyncClient",
        lambda **kwargs: FakeAsyncClient(
            FakeResponse(402, {"error": {"code": 402, "message": "Insufficient credits"}})
        ),
    )

    with pytest.raises(ValueError, match="credits"):
        asyncio.run(transcriber.transcribe_file(mp3_file, OPENROUTER_KEY))


def test_openrouter_without_a_shared_key_fails_before_the_call(
    monkeypatch, openrouter, mp3_file
):
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    monkeypatch.setattr(
        http_service.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("OpenRouter must not be called without a key"),
    )

    with pytest.raises(ValueError, match="No OpenRouter API key"):
        asyncio.run(transcriber.transcribe_file(mp3_file, None))


def test_openrouter_chunks_shift_segment_timings(monkeypatch, openrouter, mp3_file):
    fake = FakeAsyncClient(
        FakeResponse(
            200,
            {
                "text": "hi",
                "duration": 10.0,
                "segments": [{"id": 0, "start": 1.0, "end": 2.0, "text": "hi"}],
            },
        )
    )
    monkeypatch.setattr(http_service.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(transcriber.transcribe_chunks([mp3_file, mp3_file], OPENROUTER_KEY))

    assert [segment["start"] for segment in result["segments"]] == [1.0, 11.0]
    assert result["duration"] == 20.0


def test_the_shared_key_serves_openrouter_and_the_user_key_serves_groq(monkeypatch):
    user = User(groq_key=KEY, label="Alice")
    monkeypatch.setattr(settings, "openrouter_api_key", OPENROUTER_KEY)

    monkeypatch.setattr(settings, "transcription_provider", "groq")
    assert transcriber.resolve_transcription_key(user) == KEY

    monkeypatch.setattr(settings, "transcription_provider", "openrouter")
    assert transcriber.resolve_transcription_key(user) == OPENROUTER_KEY
    assert transcriber.resolve_transcription_key(None) == OPENROUTER_KEY


