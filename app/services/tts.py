import asyncio
import json
import logging
import re
from pathlib import Path

from app.config import settings
from app.services.converter import get_duration, _run_ffmpeg


class TTSAuthError(Exception):
    """The TTS provider rejected the server API key (401/403)."""


class TTSError(Exception):
    """A TTS request failed after all retries."""


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

# In-memory progress of running TTS jobs: recording_id -> {"chunks_done", "chunks_total"}
tts_progress: dict[str, dict] = {}


def get_tts_progress(recording_id: str) -> dict:
    return dict(tts_progress.get(recording_id, {}))


def set_tts_progress(recording_id: str, chunks_done: int, chunks_total: int) -> None:
    tts_progress[recording_id] = {"chunks_done": chunks_done, "chunks_total": chunks_total}


def normalize_text(raw: str) -> str:
    """Squash whitespace so the text chunks cleanly: paragraphs stay apart."""
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.S), " "),            # fenced code blocks
    (re.compile(r"`([^`]*)`", re.S), r"\1"),          # inline code -> its content
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),       # images: drop entirely
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),    # links -> anchor text
    (re.compile(r"\[\d+\]"), " "),                    # footnotes like [12]
    (re.compile(r"https?://\S+"), " "),               # bare URLs
    (re.compile(r"<[^>\n]+>"), " "),                  # HTML/XML tags
    (re.compile(r"^#{1,6}\s*", re.M), ""),            # ATX headers
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),          # block quotes
    (re.compile(r"^\s{0,3}[-*+]\s+", re.M), ""),      # bullet list markers
    (re.compile(r"^\s{0,3}\d+[.)]\s+", re.M), ""),    # ordered list markers
    (re.compile(r"^\s{0,3}\|[-: |]+\|\s*$", re.M), ""),  # table separator rows
    (re.compile(r"\|", ), " "),                        # table cell pipes
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),          # bold
    (re.compile(r"__([^_]+)__"), r"\1"),
    (re.compile(r"\*([^*\n]+)\*"), r"\1"),            # italic
    (re.compile(r"(?<![\w\\])_([^_\n]+)_(?!\w)"), r"\1"),
    (re.compile(r"~~([^~]+)~~"), r"\1"),              # strikethrough
    (re.compile(r"[-=*_{2,}]{3,}"), " "),             # horizontal rules
    (re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]"), " "),  # emoji & dingbats
    (re.compile(r"[*_#~`^]+"), " "),                  # leftover inline symbols
]


def clean_text_for_tts(raw: str) -> str:
    """Strip Markdown markup, HTML tags, URLs, emoji and other noise that TTS
    must not read aloud; keeps paragraphs and regular punctuation intact."""
    text = raw
    for pattern, replacement in _MD_PATTERNS:
        text = pattern.sub(replacement, text)
    # Collapse runs of spaces/tabs inside lines; keep paragraph breaks (\n\n).
    text = "\n".join(
        re.sub(r"[ \t]+", " ", line).strip()
        for line in re.sub(r"\n{3,}", "\n\n", text).split("\n")
    )
    return text


def chunk_text(text: str, max_chars: int = None) -> list[str]:
    """Split text into chunks of at most max_chars at sentence (or word) boundaries.

    Paragraphs are the preferred cut point, then sentences, then any whitespace,
    then a hard cut for a single word longer than the limit.
    """
    max_chars = max_chars or settings.tts_chunk_chars
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks = []
    for paragraph in re.split(r"\n{2,}|\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            chunks.append(paragraph)
            continue
        sentences = _SENTENCE_END.split(paragraph)
        current = ""
        for sentence in sentences:
            sentence = sentence.strip()
            while len(sentence) > max_chars:
                # A single oversized sentence: flush the buffer, then hard-cut
                # the sentence itself, preferring a whitespace boundary.
                if current:
                    chunks.append(current.strip())
                    current = ""
                cut = _hard_cut(sentence, max_chars)
                chunks.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if not sentence:
                continue
            candidate = f"{current} {sentence}".strip() if current else sentence
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current.strip())
                current = sentence
        if current.strip():
            chunks.append(current.strip())
    return chunks


def _hard_cut(sentence: str, max_chars: int) -> int:
    window = sentence[: max_chars + 1]
    cut = max(window.rfind(" "), window.rfind("-"))
    if cut <= 0:
        cut = max_chars
    return cut


class EdgeTTSProvider:
    """Free Microsoft Edge voices over the unofficial Read-Aloud protocol."""

    name = "edge"
    audio_format = "mp3"

    async def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        import edge_tts  # lazy: keeps the module importable without the package

        await edge_tts.Communicate(text, voice).save(str(out_path))


async def _post_tts_api(
    url: str,
    headers: dict,
    json_payload: dict | None,
    content: bytes | None,
    api_label: str,
    out_path: Path,
    transform=None,
) -> None:
    """Shared REST call for the paid providers (OpenAI / xAI) with 429/401 handling."""
    import httpx

    for attempt in range(settings.tts_max_retries):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(url, headers=headers, **({"json": json_payload} if json_payload is not None else {"content": content}))
        except httpx.HTTPError as exc:
            if attempt == settings.tts_max_retries - 1:
                raise TTSError(f"{api_label} request failed: {exc}") from exc
            await asyncio.sleep(1.0 * (attempt + 1))
            continue

        if response.status_code in (401, 403):
            raise TTSAuthError(f"{api_label} rejected the server API key ({response.status_code})")
        if response.status_code == 429:
            # Respect Retry-After when the provider sends it, then let the
            # caller's retry loop (or our own next attempt) try again.
            retry_after = _parse_retry_after(response)
            if attempt == settings.tts_max_retries - 1:
                raise TTSError(f"{api_label} rate limit hit after {settings.tts_max_retries} attempts")
            await asyncio.sleep(retry_after)
            continue
        if response.status_code >= 400:
            if attempt == settings.tts_max_retries - 1:
                raise TTSError(
                    f"{api_label} error {response.status_code}: {response.text[:200]}"
                )
            await asyncio.sleep(1.0 * (attempt + 1))
            continue

        out_path.write_bytes(transform(response.content) if transform else response.content)
        return
    raise TTSError(f"{api_label} request failed after {settings.tts_max_retries} attempts")


def _parse_retry_after(response) -> float:
    try:
        return max(0.5, float(response.headers.get("retry-after", 1.0)))
    except (TypeError, ValueError):
        return 1.0


class OpenAIProvider:
    """OpenAI speech API (gpt-4o-mini-tts / tts-1)."""

    name = "openai"
    audio_format = "mp3"

    async def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        if not settings.openai_api_key:
            raise TTSAuthError("OPENAI_API_KEY is not configured on the server")
        await _post_tts_api(
            url="https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json_payload={
                "model": settings.tts_openai_model,
                "input": text,
                "voice": voice,
                "response_format": self.audio_format,
            },
            content=None,
            api_label="OpenAI TTS",
            out_path=out_path,
        )


class GrokProvider:
    """xAI speech API (api.x.ai/v1/tts): BCP-47 language or auto, 5 built-in voices."""

    name = "grok"
    audio_format = "mp3"

    async def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        if not settings.xai_api_key:
            raise TTSAuthError("XAI_API_KEY is not configured on the server")
        await _post_tts_api(
            url="https://api.x.ai/v1/tts",
            headers={
                "Authorization": f"Bearer {settings.xai_api_key}",
                "Content-Type": "application/json",
            },
            json_payload={
                "text": text,
                "voice": voice,
                "language": "auto",
                "response_format": self.audio_format,
            },
            content=None,
            api_label="Grok TTS",
            out_path=out_path,
        )


class AzureProvider:
    """Microsoft Azure Speech (reserve provider). Same neural voice names as
    edge_tts (e.g. en-US-GuyNeural), but via the official paid REST endpoint."""

    name = "azure"
    audio_format = "mp3"

    async def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        if not settings.azure_speech_key:
            raise TTSAuthError("AZURE_SPEECH_KEY is not configured on the server")
        region = settings.azure_speech_region
        # Escape SSML special characters in the spoken text.
        escaped = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        ssml = (
            "<speak version='1.0' xml:lang='en-US'>"
            f"<voice name='{voice}'>{escaped}</voice></speak>"
        )
        await _post_tts_api(
            url=f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={
                "Ocp-Apim-Subscription-Key": settings.azure_speech_key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
            },
            json_payload=None,
            content=ssml.encode("utf-8"),
            api_label="Azure Speech",
            out_path=out_path,
        )


class GoogleProvider:
    """Google Cloud Text-to-Speech (reserve provider). Voice ids follow the
    Google naming (e.g. en-US-Neural2-C); the language code is derived from it."""

    name = "google"
    audio_format = "mp3"

    async def synthesize(self, text: str, voice: str, out_path: Path) -> None:
        if not settings.google_tts_api_key:
            raise TTSAuthError("GOOGLE_TTS_API_KEY is not configured on the server")
        import base64

        language_code = "-".join(voice.split("-")[:2]) or "en-US"

        def decode(content: bytes) -> bytes:
            return base64.b64decode(json.loads(content)["audioContent"])

        await _post_tts_api(
            url=(
                "https://texttospeech.googleapis.com/v1/text:synthesize"
                f"?key={settings.google_tts_api_key}"
            ),
            headers={"Content-Type": "application/json"},
            json_payload={
                "input": {"text": text},
                "voice": {"languageCode": language_code, "name": voice},
                "audioConfig": {"audioEncoding": "MP3"},
            },
            content=None,
            api_label="Google TTS",
            out_path=out_path,
            transform=decode,
        )


_PROVIDERS = {
    "edge": EdgeTTSProvider,
    "openai": OpenAIProvider,
    "grok": GrokProvider,
    "azure": AzureProvider,
    "google": GoogleProvider,
}


def split_voice(voice: str | None) -> tuple[str, str]:
    """Parse "<provider>:<voice>" into (provider, voice), falling back to defaults."""
    if not voice:
        voice = settings.tts_voice
    if voice and ":" in voice:
        provider, _, voice_name = voice.partition(":")
        if provider.strip() and voice_name.strip():
            return provider.strip(), voice_name.strip()
    return settings.tts_provider, (voice or settings.tts_voice)


def get_provider(name: str | None = None):
    provider_name = (name or settings.tts_provider).lower()
    cls = _PROVIDERS.get(provider_name)
    if cls is None:
        raise ValueError(f"Unknown TTS provider: {provider_name!r}")
    return cls()


async def _mock_synthesize(text: str, out_path: Path) -> None:
    """Generate a sine tone instead of a network call (no keys, no network)."""
    seconds = max(0.5, round(len(text) * 0.02, 2))
    rc, _, err = await _run_ffmpeg(
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}",
        "-b:a", "48k",
        "-ac", "1",
        str(out_path),
    )
    if rc != 0:
        raise TTSError(f"mock ffmpeg failed: {err}")


# Curated voices for the UI (spec §6): en is the default, then pt-PT / es / uk.
CURATED_VOICES = [
    {"id": "edge:en-US-GuyNeural", "name": "Guy", "lang": "en", "default": True},
    {"id": "edge:en-US-JennyNeural", "name": "Jenny", "lang": "en"},
    {"id": "edge:en-GB-RyanNeural", "name": "Ryan (UK)", "lang": "en"},
    {"id": "edge:pt-PT-DuarteNeural", "name": "Duarte", "lang": "pt-PT"},
    {"id": "edge:pt-PT-RaquelNeural", "name": "Raquel", "lang": "pt-PT"},
    {"id": "edge:es-ES-AlvaroNeural", "name": "Álvaro", "lang": "es"},
    {"id": "edge:es-ES-ElviraNeural", "name": "Elvira", "lang": "es"},
    {"id": "edge:uk-UA-OstapNeural", "name": "Остап", "lang": "uk"},
    {"id": "edge:uk-UA-PolinaNeural", "name": "Поліна", "lang": "uk"},
]

# Static voice dictionaries for the paid providers (spec §2/§11).
PROVIDER_VOICES = {
    "openai": [
        {"id": "openai:alloy", "name": "Alloy", "lang": "en"},
        {"id": "openai:ash", "name": "Ash", "lang": "en"},
        {"id": "openai:nova", "name": "Nova", "lang": "en"},
        {"id": "openai:shimmer", "name": "Shimmer", "lang": "en"},
    ],
    "grok": [
        {"id": "grok:eve", "name": "Eve", "lang": "en"},
        {"id": "grok:ara", "name": "Ara", "lang": "en"},
        {"id": "grok:rex", "name": "Rex", "lang": "en"},
        {"id": "grok:sal", "name": "Sal", "lang": "en"},
        {"id": "grok:leo", "name": "Leo", "lang": "en"},
    ],
    # Azure uses the same neural voice catalog as edge_tts, just paid/official.
    "azure": [
        {"id": f"azure:{v['id'].split(':', 1)[1]}", "name": v["name"], "lang": v["lang"]}
        for v in CURATED_VOICES
    ],
    "google": [
        {"id": "google:en-US-Neural2-C", "name": "Neural2-C (EN)", "lang": "en"},
        {"id": "google:en-US-Neural2-J", "name": "Neural2-J (EN)", "lang": "en"},
        {"id": "google:pt-PT-Wavenet-B", "name": "Wavenet-B (PT)", "lang": "pt-PT"},
        {"id": "google:es-ES-Neural2-A", "name": "Neural2-A (ES)", "lang": "es"},
        {"id": "google:uk-UA-Wavenet-A", "name": "Wavenet-A (UK)", "lang": "uk"},
    ],
}


def curated_voices() -> list[dict]:
    """Voices of the active provider: curated list for edge, static dict for paid."""
    if settings.tts_provider in PROVIDER_VOICES:
        voices = [dict(v) for v in PROVIDER_VOICES[settings.tts_provider]]
        default = settings.tts_voice
        for item in voices:
            item["default"] = item["id"] == default
        if not any(item["default"] for item in voices) and voices:
            voices[0]["default"] = True
        return voices
    if settings.tts_provider != "edge":
        return []
    default = settings.tts_voice
    voices = []
    for voice in CURATED_VOICES:
        item = dict(voice)
        item.setdefault("default", item["id"] == default)
        voices.append(item)
    return voices


async def synthesize_chunk(provider, text: str, voice: str, out_path: Path) -> None:
    """Synthesize one chunk with retries and the configured inter-request pause."""
    for attempt in range(settings.tts_max_retries):
        if settings.tts_request_interval:
            await asyncio.sleep(settings.tts_request_interval)
        try:
            if settings.mock_tts:
                await _mock_synthesize(text, out_path)
            else:
                await provider.synthesize(text, voice, out_path)
            return
        except TTSAuthError:
            raise
        except Exception as exc:
            logging.warning("TTS chunk attempt %d failed: %s", attempt + 1, exc)
            if attempt == settings.tts_max_retries - 1:
                raise TTSError(f"TTS synthesis failed after {settings.tts_max_retries} attempts: {exc}") from exc
            await asyncio.sleep(1.0 * (attempt + 1))


async def concat_chunks(chunk_paths: list[Path], out_path: Path) -> None:
    """Glue the per-chunk audio files into one MP3 at the target bitrate."""
    if len(chunk_paths) == 1:
        chunk_paths[0].replace(out_path)
        return
    list_file = out_path.parent / "chunks" / "concat.txt"
    lines = "\n".join(f"file '{path.as_posix()}'" for path in chunk_paths)
    list_file.write_text(lines, encoding="utf-8")
    rc, _, err = await _run_ffmpeg(
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-b:a", f"{settings.target_mp3_kbps}k",
        "-ac", "1",
        str(out_path),
    )
    safe_unlink(list_file)
    if rc != 0:
        raise TTSError(f"ffmpeg concat failed: {err}")


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
