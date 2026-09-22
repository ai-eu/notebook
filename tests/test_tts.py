import asyncio
import json

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Recording, User
from app.services import processing
from app.services.tts import (
    TTSError,
    chunk_text,
    get_provider,
    normalize_text,
    split_voice,
)

TEXT = (
    "The first sentence goes here. The second one follows it! And a third asks a question? "
    "The fourth sentence is a plain statement."
)


def test_short_text_is_one_chunk():
    assert chunk_text("Hello world. Short text.", 1000) == ["Hello world. Short text."]


def test_empty_text_gives_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_chunks_respect_the_limit_at_sentence_boundaries():
    chunks = chunk_text(TEXT, 60)
    assert len(chunks) > 1
    assert all(len(chunk) <= 60 for chunk in chunks)
    # No words are lost or broken: glue the chunks back and compare.
    assert " ".join(chunks).split() == TEXT.split()


def test_paragraphs_are_cut_before_sentences():
    text = (
        "Paragraph one stays whole.\n\n"
        "Paragraph two is a bit longer than the first and pushes the pair over the limit."
    )
    chunks = chunk_text(text, 90)
    assert chunks == [
        "Paragraph one stays whole.",
        "Paragraph two is a bit longer than the first and pushes the pair over the limit.",
    ]


def test_a_sentence_longer_than_the_limit_is_hard_cut():
    text = "word " * 250  # 1500 chars, no sentence punctuation
    chunks = chunk_text(text.strip(), 1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert " ".join(chunks).split() == text.split()


def test_normalize_text_trims_lines_and_drops_empty_ones():
    assert normalize_text("  Hello  \n\n  world.\n\n") == "Hello\nworld."


def test_provider_selection():
    assert get_provider("edge").audio_format == "mp3"
    # Paid providers are registered since phase 4
    assert get_provider("openai").name == "openai"
    assert get_provider("grok").name == "grok"


def test_split_voice_falls_back_to_config(monkeypatch):
    monkeypatch.setattr(settings, "tts_provider", "edge")
    monkeypatch.setattr(settings, "tts_voice", "edge:en-US-JennyNeural")
    assert split_voice("edge:pt-PT-RaquelNeural") == ("edge", "pt-PT-RaquelNeural")
    assert split_voice(None) == ("edge", "en-US-JennyNeural")
    assert split_voice("en-GB-RyanNeural") == ("edge", "en-GB-RyanNeural")


async def _create_tts_recording() -> str:
    async with AsyncSessionLocal() as db:
        user = User(groq_key="gsk_" + "a" * 52, label="Alice")
        db.add(user)
        await db.commit()
        recording = Recording(
            user_id=user.id,
            recording_id="tts-1",
            folder_path="1/tts-1",
            original_filename="note.txt",
            status="pending",
            kind="tts",
        )
        db.add(recording)
        await db.commit()
        return recording.recording_id


async def _load(recording_id: str) -> Recording:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one()


async def _fail_synth(*args):
    raise AssertionError("must not be called")


def _stub_tts_pipeline(monkeypatch, tmp_path, *, text: str = TEXT, synth=None):
    folder = tmp_path / "recording"
    folder.mkdir()
    (folder / "original.tmp").write_text(text, encoding="utf-8")

    calls = {"chunks": [], "voice": None}

    async def fake_synth(provider, chunk, voice, out_path):
        if synth is not None:
            await synth(provider, chunk, voice, out_path)
        calls["chunks"].append(chunk)
        calls["voice"] = voice
        out_path.write_bytes(b"fake audio")

    async def fake_concat(paths, out_path):
        out_path.write_bytes(b"".join(p.read_bytes() for p in paths))

    monkeypatch.setattr(processing, "resolve_recording_path", lambda path: folder)
    monkeypatch.setattr(processing, "synthesize_chunk", fake_synth)
    monkeypatch.setattr(processing, "get_duration", _fake_duration)
    monkeypatch.setattr(processing, "concat_chunks", fake_concat)
    return folder, calls


async def _fake_duration(path):
    return 2.5


def test_process_tts_full_pipeline(monkeypatch, tmp_path):
    folder, calls = _stub_tts_pipeline(monkeypatch, tmp_path)
    expected_chunks = chunk_text(TEXT, settings.tts_chunk_chars)

    async def scenario():
        recording_id = await _create_tts_recording()
        await processing.process_tts(recording_id, voice="edge:en-US-JennyNeural")
        recording = await _load(recording_id)

        assert recording.status == "done"
        assert recording.kind == "tts"
        assert recording.tts_model == "edge"
        assert recording.tts_voice == "en-US-JennyNeural"
        assert calls["chunks"] == expected_chunks
        assert calls["voice"] == "en-US-JennyNeural"
        assert recording.duration == round(2.5 * len(expected_chunks), 3)
        assert (folder / "audio.mp3").read_bytes() == b"fake audio" * len(expected_chunks)

        transcript = json.loads((folder / "transcript.json").read_text(encoding="utf-8"))
        assert transcript["duration"] == recording.duration
        starts = [segment["start"] for segment in transcript["segments"]]
        ends = [segment["end"] for segment in transcript["segments"]]
        assert starts == [round(2.5 * i, 3) for i in range(len(starts))]
        assert ends == [round(2.5 * (i + 1), 3) for i in range(len(ends))]
        assert " ".join(s["text"] for s in transcript["segments"]).split() == TEXT.split()

        assert (folder / "formatted.txt").read_text(encoding="utf-8") == TEXT
        # Inputs are cleaned up, only the deliverables stay.
        assert not (folder / "original.tmp").exists()
        assert not (folder / "chunks").exists()

    asyncio.run(scenario())


def test_process_tts_marks_errors(monkeypatch, tmp_path):
    async def failing_synth(provider, chunk, voice, out_path):
        raise TTSError("boom")

    folder, _ = _stub_tts_pipeline(monkeypatch, tmp_path, synth=failing_synth)

    async def scenario():
        recording_id = await _create_tts_recording()
        await processing.process_tts(recording_id, voice="edge:en-US-GuyNeural")
        recording = await _load(recording_id)
        assert recording.status == "error"
        assert "boom" in recording.error_message
        # Diagnostics stay: the original text is kept.
        assert (folder / "original.tmp").exists()

    asyncio.run(scenario())


def test_process_tts_rejects_empty_text(monkeypatch, tmp_path):
    folder, _ = _stub_tts_pipeline(monkeypatch, tmp_path, text="   \n\n  ")

    async def scenario():
        recording_id = await _create_tts_recording()
        await processing.process_tts(recording_id)
        recording = await _load(recording_id)
        assert recording.status == "error"
        assert "Empty" in recording.error_message

    asyncio.run(scenario())


def test_process_tts_rejects_unknown_provider(monkeypatch, tmp_path):
    _stub_tts_pipeline(monkeypatch, tmp_path)

    async def scenario():
        recording_id = await _create_tts_recording()
        await processing.process_tts(recording_id, voice="watson:some-voice")
        recording = await _load(recording_id)
        assert recording.status == "error"
        assert "Unknown TTS provider" in recording.error_message

    asyncio.run(scenario())


