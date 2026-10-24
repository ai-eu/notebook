import asyncio

import pytest

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


SEGMENTED = {
    "text": "hello world how are you. Mr. Smith left yesterday and came back later.",
    "segments": [
        {"id": 0, "start": 0.0, "end": 2.0, "text": "hello world"},
        {"id": 1, "start": 2.0, "end": 4.0, "text": "how are you. Mr. Smith left yesterday"},
        {"id": 2, "start": 4.0, "end": 6.0, "text": "and came back later."},
    ],
}


def test_sentence_units_follow_the_lines_of_the_formatted_text():
    """The player and the translation share the formatted lines, so pairs match."""
    formatted = "hello world how are you.\nMr. Smith left yesterday and came back later."

    units = formatter.build_sentence_units(SEGMENTED, formatted)

    assert [unit["text"] for unit in units] == formatted.split("\n")
    assert [unit["paragraph"] for unit in units] == [0, 0]
    assert units[0]["start"] == 0.0
    assert units[0]["end"] == pytest.approx(2.0 + (11 / 37) * 2, abs=0.002)
    assert units[1]["start"] == pytest.approx(2.0 + (13 / 37) * 2, abs=0.002)
    assert units[1]["end"] == pytest.approx(5.9, abs=0.002)


def test_sentence_units_keep_paragraphs_and_interpolate_rephrased_lines():
    transcript = {
        "text": "one. two. three.",
        "segments": [{"id": 0, "start": 0.0, "end": 3.0, "text": "one. two. three."}],
    }
    formatted = "one.\n\nSome totally new wording.\nthree."

    units = formatter.build_sentence_units(transcript, formatted)

    assert [unit["text"] for unit in units] == ["one.", "Some totally new wording.", "three."]
    assert [unit["paragraph"] for unit in units] == [0, 1, 1]
    assert units[0]["start"] == 0.0
    # The rephrased line takes the free time between its neighbours.
    assert units[1]["start"] == pytest.approx(0.5625, abs=0.002)
    assert units[1]["end"] == pytest.approx(1.875, abs=0.002)
    assert units[2]["end"] == pytest.approx(2.8125, abs=0.002)


def test_sentence_units_without_a_transcript_get_no_timings():
    units = formatter.build_sentence_units(None, "First line.\nSecond line.")

    assert [unit["text"] for unit in units] == ["First line.", "Second line."]
    assert [(unit["start"], unit["end"]) for unit in units] == [(0.0, 0.0), (0.0, 0.0)]
