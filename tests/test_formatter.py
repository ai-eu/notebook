import asyncio

from app.config import settings
from app.services import formatter
from conftest import FakeAsyncClient, FakeResponse

KEY = "gsk_" + "a" * 52

TRANSCRIPT = {
    "text": "hello world. this is a test.",
    "segments": [
        {"id": 0, "start": 0.0, "end": 2.0, "text": "hello world."},
        {"id": 1, "start": 2.2, "end": 4.0, "text": "this is a test."},
    ],
}


def _forbid_groq(monkeypatch):
    def explode(**kwargs):
        raise AssertionError("Groq must not be called")

    monkeypatch.setattr(formatter.httpx, "AsyncClient", explode)


def test_formats_locally_when_smart_formatting_has_no_key(monkeypatch):
    _forbid_groq(monkeypatch)

    text = asyncio.run(formatter.format_transcript(TRANSCRIPT, api_key=None, use_groq=True))

    assert text == "hello world.\nthis is a test."


def test_formats_locally_without_smart_formatting(monkeypatch):
    _forbid_groq(monkeypatch)

    text = asyncio.run(formatter.format_transcript(TRANSCRIPT))

    assert text == "hello world.\nthis is a test."


def test_smart_split_uses_the_callers_key(monkeypatch):
    long_sentence = " ".join(["word"] * 80)
    transcript = {
        "text": long_sentence,
        "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": long_sentence}],
    }
    fake = FakeAsyncClient(
        FakeResponse(200, {"choices": [{"message": {"content": "Short sentence.\nAnother one."}}]})
    )
    monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)

    text = asyncio.run(formatter.format_transcript(transcript, api_key=KEY, use_groq=True))

    assert text == "Short sentence.\nAnother one."
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    assert fake.requests[0]["json"]["model"] == settings.groq_chat_model
    assert fake.requests[0]["url"].endswith("/chat/completions")


def test_long_lines_are_left_alone_without_a_key(monkeypatch):
    long_sentence = " ".join(["word"] * 80)
    transcript = {
        "text": long_sentence,
        "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": long_sentence}],
    }
    _forbid_groq(monkeypatch)

    text = asyncio.run(formatter.format_transcript(transcript, api_key=None, use_groq=True))

    assert text == long_sentence
