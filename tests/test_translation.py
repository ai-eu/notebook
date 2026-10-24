import asyncio

import httpx
from sqlalchemy import select
from app.config import settings
from app.database import AsyncSessionLocal
from app.main import app
from app.models import Recording, User
from app.services import formatter
from conftest import FakeAsyncClient, FakeResponse

KEY = "gsk_" + "a" * 52


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _signed_in_user_id() -> int:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User))).scalars().first().id


async def _create_recording(user_id: int, recording_id: str = "rec-1"):
    folder = settings.data_dir_absolute / str(user_id) / recording_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "audio.mp3").write_bytes(b"lecture")
    (folder / "transcript.json").write_text('{"text": "hi", "segments": []}', encoding="utf-8")
    (folder / "formatted.txt").write_text("Hello world.\nThis is a test.\nGoodbye.", encoding="utf-8")
    async with AsyncSessionLocal() as db:
        db.add(
            Recording(
                user_id=user_id,
                recording_id=recording_id,
                folder_path=f"{user_id}/{recording_id}",
                original_filename="lecture.m4a",
                status="done",
            )
        )
        await db.commit()
    return folder


def test_translate_languages_endpoint():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            res = await client.get("/api/translate")
            assert res.status_code == 200
            data = res.json()
            assert data["enabled"] is True
            assert data["default"] == "uk"
            codes = [lang["code"] for lang in data["languages"]]
            assert "uk" in codes
            assert "pt-PT" in codes
            assert "es" in codes
            assert "en" in codes

    asyncio.run(scenario())


def test_transcript_page_renders_translation_columns():
    """The page ships the toggle button and both column containers for translation.js."""
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            await _create_recording(user_id)

            page = await client.get("/t/rec-1")
            assert page.status_code == 200
            assert 'id="toggle-translation"' in page.text
            assert 'id="translation-lang"' in page.text
            assert 'id="original-sentences"' in page.text
            assert 'id="translation-column"' in page.text
            assert 'id="translated-sentences"' in page.text
            assert "window.TRANSLATION_ENABLED = true" in page.text

    asyncio.run(scenario())


def test_translate_returns_cached_translation():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            folder = await _create_recording(user_id)
            # Pre-create the cache file.
            (folder / "formatted-uk.txt").write_text("Привіт svit.\nЦе тест.\nДо побачення.", encoding="utf-8")

            res = await client.get("/api/translate/rec-1?lang=uk")
            assert res.status_code == 200
            data = res.json()
            assert data["cached"] is True
            assert data["language"] == "uk"
            assert data["sentences"] == ["Привіт svit.", "Це тест.", "До побачення."]

    asyncio.run(scenario())


def test_translate_unsupported_language_rejected():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            await _create_recording(user_id)

            res = await client.get("/api/translate/rec-1?lang=xx")
            assert res.status_code == 400

    asyncio.run(scenario())


def test_translate_no_formatted_txt_returns_404():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            folder = settings.data_dir_absolute / str(user_id) / "rec-no-fmt"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "audio.mp3").write_bytes(b"lecture")
            (folder / "transcript.json").write_text('{"text": "hi", "segments": []}', encoding="utf-8")
            async with AsyncSessionLocal() as db:
                db.add(
                    Recording(
                        user_id=user_id,
                        recording_id="rec-no-fmt",
                        folder_path=f"{user_id}/rec-no-fmt",
                        original_filename="lecture.m4a",
                        status="done",
                    )
                )
                await db.commit()

            res = await client.get("/api/translate/rec-no-fmt?lang=uk")
            assert res.status_code == 404

    asyncio.run(scenario())


def test_translate_sentences_function_batches(monkeypatch):
    """translate_sentences sends batches and returns 1:1 aligned translations."""
    sentences = ["Hello world.", "This is a test.", "Goodbye.", "One more sentence."]
    # Single batch of 4: batch_size >= len(sentences).
    fake = FakeAsyncClient(
        FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": "Привіт светі.\nЦе тест.\nДо побачення.\nІ ще одне речення."
                        }
                    }
                ]
            },
        )
    )
    monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(
        formatter.translate_sentences(sentences, "uk", KEY, batch_size=4)
    )

    assert result == ["Привіт светі.", "Це тест.", "До побачення.", "І ще одне речення."]
    assert len(fake.requests) == 1
    assert fake.requests[0]["json"]["model"] == settings.groq_chat_model
    assert fake.requests[0]["headers"]["Authorization"] == f"Bearer {KEY}"


def test_translate_sentences_pads_when_model_returns_fewer(monkeypatch):
    """If Groq returns fewer lines than requested, pad with empty strings."""
    sentences = ["First.", "Second.", "Third."]
    fake = FakeAsyncClient(
        FakeResponse(200, {"choices": [{"message": {"content": "Перше.\nДруге."}}]})
    )
    monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(formatter.translate_sentences(sentences, "uk", KEY))

    assert result == ["Перше.", "Друге.", ""]


def test_translate_sentences_no_key_raises():
    async def scenario():
        try:
            await formatter.translate_sentences(["Hello"], "uk", None)
            assert False, "Expected TranslationError"
        except formatter.TranslationError:
            pass

    asyncio.run(scenario())
