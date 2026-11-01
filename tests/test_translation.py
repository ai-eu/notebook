import asyncio
import json
import time

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


async def _translate(client, recording_id: str, lang: str = "uk", attempts: int = 60):
    """Poll the endpoint the way the page does: 202 while the job runs."""
    for _ in range(attempts):
        res = await client.get(f"/api/translate/{recording_id}?lang={lang}")
        if res.status_code != 202:
            return res
        await asyncio.sleep(0.05)
    raise AssertionError("the translation job did not finish")


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
    """The page ships the compact language dropdown and both column containers."""
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            await _create_recording(user_id)

            page = await client.get("/t/rec-1")
            assert page.status_code == 200
            assert 'id="translation-lang"' in page.text
            assert 'id="original-sentences"' in page.text
            assert 'id="translation-column"' in page.text
            assert 'id="translated-sentences"' in page.text
            assert "window.TRANSLATION_ENABLED = true" in page.text
            # No language is preselected, there is no toggle button and no captions.
            assert '<option value="" selected>🌐</option>' in page.text
            assert 'id="toggle-translation"' not in page.text
            assert 'class="column-header"' not in page.text
            # Languages are listed as short codes in the configured order.
            for code in settings.translation_target_languages:
                assert f'<option value="{code}">{code.split("-")[0]}</option>' in page.text
            positions = [
                page.text.index(f'<option value="{code}"')
                for code in settings.translation_target_languages
            ]
            assert positions == sorted(positions)

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


def test_translate_sentences_rejects_a_wrong_line_count(monkeypatch):
    """A mismatched response is retried, then reported instead of padded silently."""
    sentences = ["First.", "Second.", "Third."]
    fake = FakeAsyncClient(
        FakeResponse(200, {"choices": [{"message": {"content": "Перше.\nДруге."}}]})
    )
    monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)

    try:
        asyncio.run(formatter.translate_sentences(sentences, "uk", KEY))
        assert False, "Expected TranslationError"
    except formatter.TranslationError as exc:
        assert "2 lines for 3 sentences" in str(exc)

    assert len(fake.requests) == 2


def test_translate_sentences_no_key_raises():
    async def scenario():
        try:
            await formatter.translate_sentences(["Hello"], "uk", None)
            assert False, "Expected TranslationError"
        except formatter.TranslationError:
            pass

    asyncio.run(scenario())


def test_translate_reports_the_groq_reason_in_detail(monkeypatch):
    """A Groq failure becomes HTTP 502 and the reason travels in `detail` for the UI."""
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            await _create_recording(user_id, "rec-groq-fail")
            fake = FakeAsyncClient(
                FakeResponse(400, {"error": {"message": "model not found"}})
            )
            monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)

            res = await _translate(client, "rec-groq-fail")

            assert res.status_code == 502
            assert "Groq API error 400" in res.json()["detail"]

    asyncio.run(scenario())


TRANSLATED = "Привіт светі.\nЦе тест.\nДо побачення."


def test_translate_returns_paragraph_breaks(monkeypatch):
    """Paragraphs of formatted.txt survive into the payload and the cache file."""
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            folder = await _create_recording(user_id, "rec-para")
            (folder / "formatted.txt").write_text(
                "Hello world.\n\nThis is a test.\nGoodbye.", encoding="utf-8"
            )
            fake = FakeAsyncClient(
                FakeResponse(200, {"choices": [{"message": {"content": TRANSLATED}}]})
            )
            monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)

            res = await _translate(client, "rec-para")

            assert res.status_code == 200
            data = res.json()
            assert data["sentences"] == TRANSLATED.split("\n")
            assert data["paragraphs"] == [0, 1, 1]
            # The background job stores the artifact first, so the polled answer is the
            # cached copy.
            assert data["cached"] is True
            assert (folder / "formatted-uk.txt").read_text(encoding="utf-8") == (
                "Привіт светі.\n\nЦе тест.\nДо побачення.\n"
            )

    asyncio.run(scenario())


def test_translate_rebuilds_a_cache_with_a_different_line_count(monkeypatch):
    """A cache from another sentence split is dropped instead of pairing wrong lines."""
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            folder = await _create_recording(user_id, "rec-stale")
            (folder / "formatted-uk.txt").write_text("Застарілий переклад.", encoding="utf-8")
            fake = FakeAsyncClient(
                FakeResponse(200, {"choices": [{"message": {"content": TRANSLATED}}]})
            )
            monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)

            res = await _translate(client, "rec-stale")

            assert res.status_code == 200
            data = res.json()
            # The job stores the artifact before the page polls it back.
            assert data["cached"] is True
            assert data["sentences"] == TRANSLATED.split("\n")

    asyncio.run(scenario())


def test_transcript_page_renders_the_formatted_lines_with_timings():
    """The original column is built from formatted.txt, exactly like the translation."""
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            folder = await _create_recording(user_id, "rec-units")
            (folder / "transcript.json").write_text(
                json.dumps(
                    {
                        "text": "hello world how are you.",
                        "segments": [
                            {"id": 0, "start": 0.0, "end": 1.0, "text": "hello world"},
                            {"id": 1, "start": 1.0, "end": 2.0, "text": "how are you."},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (folder / "formatted.txt").write_text("hello world\n\nhow are you.", encoding="utf-8")

            page = await client.get("/t/rec-units")

            assert page.status_code == 200
            assert "window.SENTENCE_UNITS" in page.text
            assert page.text.count('class="sentence sentence-line"') == 2
            assert page.text.count('class="transcript-paragraph"') == 2
            # Timings come from the segments, so the player still works.
            assert 'data-start="0.000"' in page.text
            assert 'data-end="1.000"' in page.text
            assert 'data-start="1.000"' in page.text
            assert 'data-end="1.917"' in page.text

    asyncio.run(scenario())



def test_translate_answers_202_while_the_job_runs(monkeypatch):
    """A long transcript is translated in the background and the page polls it."""
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            await _create_recording(user_id, "rec-progress")
            fake = FakeAsyncClient(
                FakeResponse(200, {"choices": [{"message": {"content": TRANSLATED}}]})
            )
            monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)

            first = await client.get("/api/translate/rec-progress?lang=uk")

            assert first.status_code == 202
            body = first.json()
            assert body["status"] == "running"
            assert body["total"] == 3

            done = await _translate(client, "rec-progress")
            assert done.status_code == 200
            assert done.json()["sentences"] == TRANSLATED.split("\n")
            # The finished translation is served from the cache afterwards.
            again = await client.get("/api/translate/rec-progress?lang=uk")
            assert again.status_code == 200
            assert again.json()["cached"] is True

    asyncio.run(scenario())


def test_translate_never_asks_for_more_output_tokens_than_groq_allows(monkeypatch):
    """Groq rejects a request whose max_tokens exceeds the per-minute output budget."""
    sentences = ["Hello world.", "This is a test."]
    fake = FakeAsyncClient(
        FakeResponse(200, {"choices": [{"message": {"content": "Привіт светі.\nЦе тест."}}]})
    )
    monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)
    monkeypatch.setattr(settings, "translation_max_output_tokens", 300)

    asyncio.run(formatter.translate_sentences(sentences, "uk", KEY, batch_size=2))

    assert fake.requests[0]["json"]["max_tokens"] <= 300


class _SequencedClient:
    """Fake client that answers with a prepared response sequence."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.requests: list[dict] = []

    async def post(self, url, headers=None, json=None, data=None, files=None, **kwargs):
        self.requests.append({"url": url, "json": json, "headers": headers or {}})
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def test_translate_waits_out_a_throttled_batch(monkeypatch):
    """A 429 is retried after the asked pause instead of failing the translation."""
    sentences = ["Hello world.", "This is a test."]
    fake = _SequencedClient(
        [
            FakeResponse(429, {"error": {"message": "slow down"}}, {"retry-after": "1"}),
            FakeResponse(200, {"choices": [{"message": {"content": "Привіт светі.\nЦе тест."}}]}),
        ]
    )
    monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)
    monkeypatch.setattr(formatter, "THROTTLE_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(formatter, "MAX_THROTTLE_WAIT_SECONDS", 0.01)

    result = asyncio.run(formatter.translate_sentences(sentences, "uk", KEY, batch_size=2))

    assert result == ["Привіт светі.", "Це тест."]
    assert len(fake.requests) == 2


def test_translate_reports_a_persistent_rate_limit_as_429(monkeypatch):
    """A key that stays throttled is reported as 429, not as a gateway error."""
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            user_id = await _signed_in_user_id()
            await _create_recording(user_id, "rec-throttled")
            fake = FakeAsyncClient(
                FakeResponse(429, {"error": {"message": "rate limit"}}, {"retry-after": "0"})
            )
            monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)
            monkeypatch.setattr(formatter, "THROTTLE_BACKOFF_SECONDS", 0.0)
            # The retries are what this test covers: keep the minute budget out of the way.
            monkeypatch.setattr(settings, "translation_output_tokens_per_minute", 0)
            monkeypatch.setattr(settings, "translation_tokens_per_minute", 0)


            res = await _translate(client, "rec-throttled")


            assert res.status_code == 429
            assert "rate limit" in res.json()["detail"].lower()
            assert len(fake.requests) == formatter.THROTTLE_ATTEMPTS

    asyncio.run(scenario())


def test_translate_prefers_the_shared_gemini_key(monkeypatch):
    """One Google key serves every account; the Groq key is only the fallback."""
    sentences = ["Hello world.", "This is a test."]
    fake = FakeAsyncClient(
        FakeResponse(200, {"choices": [{"message": {"content": "Привіт светі.\nЦе тест."}}]})
    )
    monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-test-key")
    monkeypatch.setattr(settings, "gemini_model", "gemini-2.5-flash")

    result = asyncio.run(formatter.translate_sentences(sentences, "uk", KEY))

    assert result == ["Привіт светі.", "Це тест."]
    request = fake.requests[0]
    assert request["url"] == f"{settings.gemini_base_url}/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer AIza-test-key"
    assert request["json"]["model"] == "gemini-2.5-flash"
    # Gemini is not a reasoning-only model: no extra payload fields, room for thinking.
    assert "reasoning_effort" not in request["json"]
    assert request["json"]["max_tokens"] <= settings.gemini_max_output_tokens


def test_translate_falls_back_to_groq_without_a_gemini_key(monkeypatch):
    sentences = ["Hello."]
    fake = FakeAsyncClient(
        FakeResponse(200, {"choices": [{"message": {"content": "Привіт."}}]})
    )
    monkeypatch.setattr(formatter.httpx, "AsyncClient", lambda **kwargs: fake)

    asyncio.run(formatter.translate_sentences(sentences, "uk", KEY))

    assert fake.requests[0]["url"] == formatter.GROQ_CHAT_URL
    assert fake.requests[0]["json"]["model"] == settings.groq_chat_model


def test_token_budget_spaces_the_batches_over_a_minute(monkeypatch):
    """The second batch of a minute waits until the window slides on."""
    monkeypatch.setattr(formatter, "TRANSLATION_WINDOW_SECONDS", 0.15)

    async def scenario():
        budget = formatter._TokenBudget(100, 0)
        await budget.acquire(60, 0)
        started = time.monotonic()
        await budget.acquire(60, 0)
        return time.monotonic() - started

    assert asyncio.run(scenario()) >= 0.1
