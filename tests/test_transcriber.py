import asyncio

import pytest

from app.config import settings
from app.services import transcriber
from app.services.groq import GroqAuthError
from conftest import FakeAsyncClient, FakeResponse

KEY = "gsk_" + "a" * 52


@pytest.fixture
def mp3_file(tmp_path):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"ID3 fake audio")
    return path


def test_sends_the_callers_key_to_groq(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    fake = FakeAsyncClient(FakeResponse(200, {"text": "hello", "segments": []}))
    monkeypatch.setattr(transcriber.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(transcriber.transcribe_file(mp3_file, KEY))

    assert result == {"text": "hello", "segments": []}
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert fake.requests[0]["url"].endswith("/audio/transcriptions")


def test_chunks_reuse_the_same_key(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    fake = FakeAsyncClient(FakeResponse(200, {"text": "hi", "segments": [], "duration": 1.0}))
    monkeypatch.setattr(transcriber.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(transcriber.transcribe_chunks([mp3_file, mp3_file], KEY))

    assert len(fake.requests) == 2
    assert all(r["headers"]["Authorization"] == f"Bearer {KEY}" for r in fake.requests)
    assert result["text"] == "hi hi"


def test_rejected_key_raises_an_auth_error(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    monkeypatch.setattr(
        transcriber.httpx,
        "AsyncClient",
        lambda **kwargs: FakeAsyncClient(FakeResponse(401, {"error": "Invalid API Key"})),
    )

    with pytest.raises(GroqAuthError):
        asyncio.run(transcriber.transcribe_file(mp3_file, KEY))


def test_other_errors_stay_plain_value_errors(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    monkeypatch.setattr(
        transcriber.httpx,
        "AsyncClient",
        lambda **kwargs: FakeAsyncClient(FakeResponse(500, {"error": "boom"})),
    )

    with pytest.raises(ValueError, match="500"):
        asyncio.run(transcriber.transcribe_file(mp3_file, KEY))


def test_a_missing_key_fails_before_calling_groq(monkeypatch, mp3_file):
    monkeypatch.setattr(settings, "mock_transcription", False)
    monkeypatch.setattr(
        transcriber.httpx,
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
        transcriber.httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("Groq must not be called in mock mode"),
    )

    result = asyncio.run(transcriber.transcribe_file(mp3_file, None))

    assert result["text"]
    assert result["duration"] == 3.0
