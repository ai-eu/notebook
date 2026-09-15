import asyncio
import logging
import re
from typing import Any

import httpx
from app.config import settings


PARAGRAPH_GAP_SECONDS = 1.5
MAX_LINE_LENGTH = 250
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


# Common abbreviations that should not break the text as a sentence end.
_ABBREVIATIONS = sorted(
    [
        "et al.",
        "e.g.",
        "E.g.",
        "i.e.",
        "I.e.",
        "Prof.",
        "prof.",
        "Mrs.",
        "mrs.",
        "Ave.",
        "ave.",
        "Blvd.",
        "blvd.",
        "etc.",
        "Etc.",
        "vol.",
        "Vol.",
        "fig.",
        "Fig.",
        "pp.",
        "vs.",
        "ch.",
        "Ch.",
        "Dr.",
        "dr.",
        "Mr.",
        "mr.",
        "Ms.",
        "ms.",
        "St.",
        "st.",
        "Rd.",
        "rd.",
        "No.",
        "no.",
        "p.",
        "P.",
    ],
    key=len,
    reverse=True,
)

# Splits into sentences: .!? (with ellipsis and closing quote),
# then a space and the next word. Transcripts often have no capitals,
# so a capital letter is not required.
_SENTENCE_RE = re.compile(r"([.!?]+[\"'»]?)\s+(?=\S)")

# Secondary split for long lines by comma/semicolon/colon.
# Capture group so the sub can put \1\n back.
_CLAUSE_SPLIT_RE = re.compile(r"([,;:])(?=\s+\S)")



def _protect_abbreviations(text: str) -> tuple[str, dict[str, str]]:
    placeholders = {}
    result = text
    for i, abbr in enumerate(_ABBREVIATIONS):
        key = f"___ABBR_{i}___"
        placeholders[key] = abbr
        pattern = re.escape(abbr)
        result = re.sub(rf"(?<!\S){pattern}(?!\S)", key, result)
    return result, placeholders


def _restore_abbreviations(text: str, placeholders: dict[str, str]) -> str:
    for key, abbr in placeholders.items():
        text = text.replace(key, abbr)
    return text


def _split_into_sentences(text: str) -> list[str]:
    if not text.strip():
        return []
    protected, placeholders = _protect_abbreviations(text)
    marked = _SENTENCE_RE.sub(r"\1\n", protected)
    return [
        _restore_abbreviations(s.strip(), placeholders)
        for s in marked.split("\n")
        if s.strip()
    ]


def _split_by_punctuation(text: str) -> list[str]:
    """Split a string by .!? without requiring a capital letter (for splitting LLM output)."""
    if not text.strip():
        return []
    protected, placeholders = _protect_abbreviations(text)
    # Split by .!? + optional closing quote + space + next non-whitespace
    marked = re.sub(r"([.!?]+[\"'»]?)\s+(?=\S)", r"\1\n", protected)
    return [
        _restore_abbreviations(s.strip(), placeholders)
        for s in marked.split("\n")
        if s.strip()
    ]


def _split_by_clauses(text: str) -> list[str]:
    """Split a sentence into clauses by ,;:. It does not use max_len — that is the caller's job."""
    if not text.strip():
        return []
    protected, placeholders = _protect_abbreviations(text)
    # Insert \n after ,;: keeping the punctuation in the first part
    marked = _CLAUSE_SPLIT_RE.sub(r"\1\n", protected)
    return [
        _restore_abbreviations(s.strip(), placeholders)
        for s in marked.split("\n")
        if s.strip()
    ]


def _build_groq_prompt(text: str, max_len: int) -> str:
    """Prompt for Groq: add punctuation and split into complete sentences <= max_len."""
    return (
        "You are a text formatter. The input is a run-on transcript without proper sentence breaks.\n\n"
        "Your task:\n"
        "1. Add missing punctuation (periods, commas, quotation marks, etc.) to form complete, meaningful sentences.\n"
        "2. Split the text into complete, meaningful sentences.\n"
        "3. Each sentence must be on its own line.\n"
        f"4. No line may exceed {max_len} characters. "
        f"If a sentence would exceed {max_len}, split it into multiple shorter complete sentences.\n"
        "5. Preserve all original words exactly — do not summarize, rephrase, omit, add numbering, bullets, or commentary.\n"
        "6. Return only the resulting lines, one per line.\n\n"
        "Example:\n"
        'Input: "hello world she said what are you doing i am reading she answered"\n'
        "Output:\n"
        "Hello world.\n"
        'She said, "What are you doing?"\n'
        "I am reading, she answered.\n\n"
        f"Text:\n{text}"
    )


def _parse_groq_response(content: str) -> list[str]:
    """Extract lines from the LLM response, dropping empties and list markers."""
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Drop possible markdown list markers
        if stripped.startswith(("- ", "* ", "• ", "1. ", "2. ", "3. ")):
            stripped = stripped[2:].strip()
        lines.append(stripped)
    return lines


def _estimate_max_tokens(text_len: int) -> int:
    """Estimate a sufficient max_tokens for a string of text_len characters.
    English is ~4 characters per token, output is usually slightly longer than input."""
    return max(300, text_len // 3 + 200)


async def _post_to_groq(payload: dict[str, Any], headers: dict[str, str], max_retries: int = 3) -> dict[str, Any] | None:
    """POST with retries on 429 and logging of HTTP errors."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(max_retries):
            response = await client.post(GROQ_CHAT_URL, json=payload, headers=headers)

            if response.status_code == 429:
                body = response.text[:500]
                retry_after = response.headers.get("retry-after") or response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else 2 ** attempt
                except ValueError:
                    wait = 2 ** attempt
                logging.warning("Groq rate limit (attempt %d): %s; retry in %.1fs", attempt + 1, body, wait)
                await asyncio.sleep(wait)
                continue

            if response.status_code in (401, 403):
                logging.warning("Groq rejected the API key (HTTP %s)", response.status_code)
                return None

            if response.status_code >= 400:
                body = response.text[:500]
                logging.warning("Groq HTTP %s: %s", response.status_code, body)
                return None

            return response.json()

    logging.warning("Groq rate limit retries exhausted")
    return None


async def _split_long_sentence_with_groq(
    text: str,
    api_key: str | None,
    max_len: int = MAX_LINE_LENGTH,
) -> list[str] | None:
    """Try to split a long sentence via the Groq Chat API with retries and a modest max_tokens."""
    if not api_key:
        return None

    payload = {
        "model": settings.groq_chat_model,
        "messages": [
            {"role": "system", "content": "You are a helpful text formatter."},
            {"role": "user", "content": _build_groq_prompt(text, max_len)},
        ],
        "temperature": 0.1,
        "max_tokens": _estimate_max_tokens(len(text)),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    logging.info("Groq smart split request: %d chars", len(text))
    data = await _post_to_groq(payload, headers)
    if not data:
        return None

    try:
        content = data["choices"][0]["message"]["content"]
        lines = _parse_groq_response(content)
        # The model may put multiple sentences in one line — split by punctuation.
        split_lines: list[str] = []
        for line in lines:
            split_lines.extend(_split_by_punctuation(line))

        # If long lines remain after periods/questions/exclamations, try ,;:
        final_lines: list[str] = []
        for line in split_lines:
            if len(line) <= max_len:
                final_lines.append(line)
            else:
                clauses = _split_by_clauses(line)
                final_lines.extend(clauses)

        if final_lines and all(len(line) <= max_len for line in final_lines):
            logging.info("Groq split accepted: %d lines", len(final_lines))
            return final_lines

        for line in final_lines:
            if len(line) > max_len:
                logging.warning(
                    "Groq split rejected: line too long (%d chars): %s...",
                    len(line),
                    line[:80],
                )
    except Exception as exc:
        logging.warning("Groq smart format failed: %s", exc)

    return None


async def _reformat_long_lines(
    text: str,
    api_key: str | None = None,
    use_groq: bool = False,
    max_len: int = MAX_LINE_LENGTH,
) -> str:
    """Split long lines locally by punctuation.
    If a clause is still longer than max_len, send it to Groq.
    Without Groq, long meaningful clauses are left unchanged (we do not wrap by spaces)."""
    if not text:
        return text
    lines = text.split("\n")
    result: list[str] = []
    for line in lines:
        if not line or len(line) <= max_len:
            result.append(line)
            continue
        clauses = _split_by_clauses(line)
        if not clauses:
            result.append(line)
            continue
        for clause in clauses:
            if len(clause) <= max_len:
                result.append(clause)
            elif use_groq and api_key:
                groq_lines = await _split_long_sentence_with_groq(clause, api_key, max_len)
                if groq_lines:
                    result.extend(groq_lines)
                else:
                    result.append(clause)
            else:
                result.append(clause)
    return "\n".join(result)


def _format_text(text: str) -> str:
    """Formatting when there are no segments — work with a single text."""
    text = " ".join(text.split())
    return "\n".join(_split_into_sentences(text))


def _build_paragraphs(segments: list[dict[str, Any]]) -> list[str]:
    paragraphs = []
    current = []
    prev_end = None

    for seg in segments:
        raw_text = seg.get("text") or ""
        text = " ".join(raw_text.split())
        if not text:
            continue

        start = float(seg.get("start", 0))
        end = float(seg.get("end", start))

        if prev_end is not None and start - prev_end > PARAGRAPH_GAP_SECONDS:
            if current:
                paragraphs.append(" ".join(current))
            current = [text]
        else:
            current.append(text)

        prev_end = end

    if current:
        paragraphs.append(" ".join(current))

    return paragraphs


async def format_transcript(
    transcript: dict[str, Any],
    api_key: str | None = None,
    use_groq: bool = False,
) -> str:
    """Format the transcript: paragraphs by pauses, sentences by .!?,
    and very long lines are additionally split locally or via Groq."""
    if use_groq and not api_key:
        logging.warning("Smart formatting is enabled, but the user has no Groq API key — smart splitting will not be used.")

    segments = transcript.get("segments") or []
    if not segments:
        text = _format_text(transcript.get("text", ""))
        return await _reformat_long_lines(text, api_key, use_groq=use_groq)

    paragraphs = _build_paragraphs(segments)
    formatted: list[str] = []

    for para in paragraphs:
        sentences = _split_into_sentences(para)
        formatted.append("\n".join(sentences) if sentences else "")

    return await _reformat_long_lines(
        "\n\n".join(p for p in formatted if p).strip(),
        api_key,
        use_groq=use_groq,
    )
