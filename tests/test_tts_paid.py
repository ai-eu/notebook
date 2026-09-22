import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.services.tts import (
    AzureProvider,
    GoogleProvider,
    GrokProvider,
    OpenAIProvider,
    TTSAuthError,
    TTSError,
    _PROVIDERS,
    clean_text_for_tts,
    curated_voices,
    get_provider,
)


def _patch_http(monkeypatch, statuses):
    """Patch httpx.AsyncClient with a canned sequence of responses; captures requests."""
    calls = []

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}
            self.text = "boom"

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, **kwargs):
            calls.append({"url": url, "headers": headers, "payload": kwargs.get("json")})
            status = statuses.pop(0)
            response = FakeResponse(status)
            if status == 429:
                response.headers = {"retry-after": "0.05"}
            if status < 400:
                response.content = b"fake-audio"
            return response

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    return calls


def test_paid_providers_are_registered():
    assert set(_PROVIDERS) == {"edge", "openai", "grok", "azure", "google"}


def test_openai_synthesize_success(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    calls = _patch_http(monkeypatch, [200])

    async def scenario():
        out = tmp_path / "chunk.mp3"
        await OpenAIProvider().synthesize("Hello", "alloy", out)
        assert out.read_bytes() == b"fake-audio"

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.openai.com/v1/audio/speech"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert calls[0]["payload"]["model"] == settings.tts_openai_model
    assert calls[0]["payload"]["input"] == "Hello"
    assert calls[0]["payload"]["voice"] == "alloy"


def test_openai_401_raises_auth_error(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "openai_api_key", "sk-bad")
    _patch_http(monkeypatch, [401])

    async def scenario():
        with pytest.raises(TTSAuthError):
            await OpenAIProvider().synthesize("Hello", "alloy", tmp_path / "c.mp3")

    asyncio.run(scenario())


def test_openai_429_then_success(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "tts_max_retries", 3)
    _patch_http(monkeypatch, [429, 200])

    async def scenario():
        out = tmp_path / "chunk.mp3"
        await OpenAIProvider().synthesize("Hello", "alloy", out)
        assert out.read_bytes() == b"fake-audio"

    asyncio.run(scenario())


def test_missing_key_raises_auth_error(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "openai_api_key", None)
    monkeypatch.setattr(settings, "xai_api_key", None)

    async def scenario():
        with pytest.raises(TTSAuthError):
            await OpenAIProvider().synthesize("Hi", "alloy", tmp_path / "c.mp3")
        with pytest.raises(TTSAuthError):
            await GrokProvider().synthesize("Hi", "eve", tmp_path / "c.mp3")

    asyncio.run(scenario())


def test_grok_synthesize_success(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "xai_api_key", "xai-test")
    calls = _patch_http(monkeypatch, [200])

    async def scenario():
        out = tmp_path / "chunk.mp3"
        await GrokProvider().synthesize("Olá", "eve", out)
        assert out.read_bytes() == b"fake-audio"

    asyncio.run(scenario())
    assert calls[0]["url"] == "https://api.x.ai/v1/tts"
    assert calls[0]["headers"]["Authorization"] == "Bearer xai-test"
    assert calls[0]["payload"]["text"] == "Olá"
    assert calls[0]["payload"]["voice"] == "eve"


def test_grok_permanent_error_raises_tts_error(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "xai_api_key", "xai-test")
    monkeypatch.setattr(settings, "tts_max_retries", 2)
    _patch_http(monkeypatch, [500, 500])

    async def scenario():
        with pytest.raises(TTSError):
            await GrokProvider().synthesize("Hi", "eve", tmp_path / "c.mp3")

    asyncio.run(scenario())


def test_curated_voices_follow_the_provider(monkeypatch):
    monkeypatch.setattr(settings, "tts_provider", "edge")
    edge = curated_voices()
    assert {v["lang"] for v in edge} == {"en", "pt-PT", "es", "uk"}

    monkeypatch.setattr(settings, "tts_provider", "openai")
    openai = curated_voices()
    assert {v["id"] for v in openai} == {
        "openai:alloy", "openai:ash", "openai:nova", "openai:shimmer",
    }
    assert sum(1 for v in openai if v["default"]) == 1

    monkeypatch.setattr(settings, "tts_provider", "grok")
    grok = curated_voices()
    assert {v["id"] for v in grok} == {
        "grok:eve", "grok:ara", "grok:rex", "grok:sal", "grok:leo",
    }


def test_azure_synthesize_sends_ssml(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "azure_speech_key", "az-key")
    monkeypatch.setattr(settings, "azure_speech_region", "westeurope")
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, **kwargs):
            captured.update({"url": url, "headers": headers, "body": kwargs.get("content")})

            class R:
                status_code = 200
                headers = {}
                text = ""
                content = b"fake-audio"

            return R()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    async def scenario():
        out = tmp_path / "chunk.mp3"
        await AzureProvider().synthesize("Hello", "en-US-GuyNeural", out)
        assert out.read_bytes() == b"fake-audio"

    asyncio.run(scenario())
    assert captured["url"] == "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1"
    assert captured["headers"]["Ocp-Apim-Subscription-Key"] == "az-key"
    assert "en-US-GuyNeural" in captured["body"].decode()


def test_azure_ssml_escaping(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "azure_speech_key", "az-key")
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, **kwargs):
            captured["body"] = kwargs.get("content")
            captured["url"] = url

            class R:
                status_code = 200
                headers = {}
                content = b"audio"
                text = ""

            return R()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    async def scenario():
        await AzureProvider().synthesize('A & B <tag> "q"', "en-US-GuyNeural", tmp_path / "c.mp3")

    asyncio.run(scenario())
    body = captured["body"].decode()
    assert "&amp;" in body and "&lt;tag&gt;" in body and "&quot;" in body


def test_google_synthesize_decodes_base64(monkeypatch, tmp_path):
    import base64 as b64

    monkeypatch.setattr(settings, "google_tts_api_key", "g-key")
    audio = b64.b64encode(b"mp3-bytes").decode()
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, **kwargs):
            calls.append({"url": url, "payload": kwargs.get("json")})

            class R:
                status_code = 200
                headers = {}
                text = ""
                content = json.dumps({"audioContent": audio}).encode()

            return R()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    async def scenario():
        out = tmp_path / "chunk.mp3"
        await GoogleProvider().synthesize("Olá", "pt-PT-Wavenet-B", out)
        assert out.read_bytes() == b"mp3-bytes"

    asyncio.run(scenario())
    assert calls[0]["url"].startswith("https://texttospeech.googleapis.com/v1/text:synthesize?key=g-key")
    assert calls[0]["payload"]["voice"] == {"languageCode": "pt-PT", "name": "pt-PT-Wavenet-B"}


def test_google_missing_key_raises_auth_error(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "google_tts_api_key", None)

    async def scenario():
        with pytest.raises(TTSAuthError):
            await GoogleProvider().synthesize("Hi", "en-US-Neural2-C", tmp_path / "c.mp3")

    asyncio.run(scenario())


def test_clean_text_strips_markdown_and_noise():
    raw = (
        "# Заголовок\n\n"
        "Смотрите ![картинка](https://x.com/i.png) и [ссылку](https://example.com) тут.\n"
        "- пункт *курсив* и **жирный** и `код`\n"
        "```\nSELECT 1;\n```\n"
        "Сноска [12] и url https://plain.example.org?a=1.\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
        "<b>html</b> теги 🎉😄\n"
        "https://x.co\n---\nконец ~~зачёркнутый~~"
    )
    cleaned = clean_text_for_tts(raw)
    assert "#" not in cleaned
    assert "*" not in cleaned
    assert "`" not in cleaned
    assert "|" not in cleaned
    assert "<" not in cleaned and ">" not in cleaned
    assert "http" not in cleaned
    assert "[" not in cleaned and "]" not in cleaned
    assert "🎉" not in cleaned
    assert "SELECT 1" not in cleaned  # fenced code dropped entirely
    assert "Заголовок" in cleaned
    assert "ссылку" in cleaned
    assert "жирный" in cleaned
    assert "конец" in cleaned
    assert "зачёркнутый" in cleaned


def test_clean_text_keeps_plain_text_and_paragraphs():
    raw = "Первая строка.\n\nВторая строка, с запятой. Цена 15$ — ок."
    assert clean_text_for_tts(raw) == raw
