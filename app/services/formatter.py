import asyncio
import json
import logging
import re
import textwrap
from typing import Any

import httpx
from app.config import settings
from app.services.http import post_with_retries

MAX_LINE_LENGTH = 250
PARAGRAPH_GAP_SECONDS = 1.5
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


class TranslationError(Exception):
    """Raised when translation via Groq fails."""


# Language names used in the UI language selector.
LANGUAGE_NAMES = {
    "uk": "Українська",
    "pt-PT": "Português",
    "es": "Español",
    "en": "English",
}


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

# All abbreviations in one alternation: one regex pass instead of one per entry.
_ABBR_RE = re.compile(
    r"(?<!\S)(?:" + "|".join(map(re.escape, _ABBREVIATIONS)) + r")(?!\S)"
)

# Splits into sentences: .!? (with ellipsis and closing quote),
# then a space and the next word. Transcripts often have no capitals,
# so a capital letter is not required.
_SENTENCE_RE = re.compile(r"([.!?]+[\"'»]?)\s+(?=\S)")

# Secondary split for long lines by comma/semicolon/colon.
# Capture group so the sub can put \1\n back.
_CLAUSE_SPLIT_RE = re.compile(r"([,;:])(?=\s+\S)")

# Words used to line the formatted lines up with the transcript timings.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

# How far ahead the matcher may look for the next word of a formatted line.
ALIGN_LOOKAHEAD = 25

# Whisper sometimes parrots its prompt or credits from its training data on
# silence and noise ("Transcription by CastingWords", "Subtitles by the
# Amara.org community"). Strip those phrases from the output.
_ARTIFACT_RE = re.compile(
    r"(?:transcri(?:ption|bed)|subtitles?|captioned|subtitling)\s+"
    r"(?:by|at)\s+(?:the\s+)?"
    r"(?:casting\s*words|amara\.?org)(?:\s+community)?\.?"
    r"|casting\s*words\.?"
    r"|amara\.?org(?:\s+community)?\.?"
    r"|(?:please\s+)?transcribe\s+with\s+proper\s+punctuation[^\n.]*\.?",
    re.IGNORECASE,
)


def strip_artifacts(text: str) -> str:
    """Remove Whisper prompt echoes and training-data credits from a text."""
    return " ".join(_ARTIFACT_RE.sub(" ", text).split())


def clean_transcript(transcript: dict[str, Any]) -> dict[str, Any]:
    """Drop Whisper artifact segments (prompt echoes, subtitle credits)
    and rebuild the full text from the surviving segments."""
    prompt_norm = " ".join((settings.whisper_prompt or "").split()).lower().rstrip(".")
    result = dict(transcript)
    raw_segments = transcript.get("segments") or []
    segments: list[dict[str, Any]] = []
    for seg in raw_segments:
        text = strip_artifacts(seg.get("text") or "")
        if not text or (prompt_norm and text.lower().rstrip(".") == prompt_norm):
            continue
        segments.append({**seg, "text": text})
    if raw_segments:
        result["segments"] = segments
        result["text"] = " ".join(s["text"] for s in segments)
    elif transcript.get("text"):
        result["text"] = strip_artifacts(transcript["text"])
    return result



def _protect_abbreviations(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def _replace(match: re.Match[str]) -> str:
        key = f"___ABBR_{len(placeholders)}___"
        placeholders[key] = match.group(0)
        return key

    return _ABBR_RE.sub(_replace, text), placeholders


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


def _split_sentences_with_offsets(text: str) -> list[tuple[int, int, str]]:
    """Split text into sentences and return their character ranges.

    Abbreviation punctuation is masked (not replaced by a different-length
    placeholder), so offsets stay valid for mapping sentences back to segments.
    """
    if not text.strip():
        return []

    masked = list(text)
    for match in _ABBR_RE.finditer(text):
        for index in range(match.start(), match.end()):
            if masked[index] in ".!?":
                masked[index] = "\x00"

    boundaries = [0]
    for match in _SENTENCE_RE.finditer("".join(masked)):
        boundaries.append(match.end())
    boundaries.append(len(text))

    result: list[tuple[int, int, str]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        raw = text[start:end]
        stripped = raw.strip()
        if not stripped:
            continue
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw) - len(raw.rstrip())
        result.append((start + leading, end - trailing, stripped))
    return result


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
    """POST with retries on 429/5xx/network errors.

    Returns None instead of raising: formatting is an optional enhancement and must
    never fail the whole transcription job.
    """
    try:
        response = await post_with_retries(
            GROQ_CHAT_URL,
            json=payload,
            headers=headers,
            timeout=30.0,
            max_retries=max_retries,
        )
    except httpx.HTTPError as exc:
        logging.warning("Groq request failed: %s", exc)
        return None

    if response.status_code in (401, 403):
        logging.warning("Groq rejected the API key (HTTP %s)", response.status_code)
        return None

    if response.status_code >= 400:
        logging.warning("Groq HTTP %s: %s", response.status_code, response.text[:500])
        return None

    try:
        return response.json()
    except ValueError:
        logging.warning("Groq returned a non-JSON response: %s", response.text[:200])
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
            split_lines.extend(_split_into_sentences(line))

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


def _iter_paragraph_segments(
    segments: list[dict[str, Any]],
) -> list[list[tuple[str, float, float, Any]]]:
    """Group segments into paragraphs by pause, keeping their timings."""
    paragraphs: list[list[tuple[str, float, float, Any]]] = []
    current: list[tuple[str, float, float, Any]] = []
    prev_end: float | None = None

    for seg in segments:
        text = " ".join((seg.get("text") or "").split())
        if not text:
            continue
        start = float(seg.get("start", 0.0) or 0.0)
        end = float(seg.get("end", start) or start)
        if end < start:
            end = start
        if prev_end is not None and start - prev_end > PARAGRAPH_GAP_SECONDS:
            if current:
                paragraphs.append(current)
            current = []
        current.append((text, start, end, seg.get("id")))
        prev_end = end

    if current:
        paragraphs.append(current)
    return paragraphs


def _interpolate_span(
    span: tuple[int, int, float, float, Any],
    position: int,
) -> float:
    text_start, text_end, seg_start, seg_end, _ = span
    if text_end <= text_start:
        return seg_start
    position = min(max(position, text_start), text_end)
    ratio = (position - text_start) / (text_end - text_start)
    return seg_start + ratio * (seg_end - seg_start)


def build_sentence_document(transcript: dict[str, Any]) -> dict[str, Any]:
    """Turn cleaned Whisper segments into canonical sentence units with exact timings.

    The result is the single source of truth for the page, the player and translation.
    It is O(n) in the transcript size and does not search for words afterwards.
    """
    segments = transcript.get("segments") or []
    duration = float(transcript.get("duration") or 0.0)
    if not duration and segments:
        duration = max(float(seg.get("end", 0.0) or 0.0) for seg in segments)

    sentences: list[dict[str, Any]] = []
    if not segments:
        text = " ".join((transcript.get("text") or "").split())
        for _, _, sentence in _split_sentences_with_offsets(text):
            sentences.append(
                {
                    "text": sentence,
                    "start": 0.0,
                    "end": 0.0,
                    "paragraph": 0,
                    "segment_ids": [],
                }
            )
        return {"version": 1, "duration": round(duration, 3), "sentences": sentences}

    for paragraph_index, paragraph in enumerate(_iter_paragraph_segments(segments)):
        parts: list[str] = []
        spans: list[tuple[int, int, float, float, Any]] = []
        cursor = 0
        for text, start, end, seg_id in paragraph:
            if parts:
                cursor += 1  # the space inserted by " ".join
            text_start = cursor
            cursor += len(text)
            parts.append(text)
            spans.append((text_start, cursor, start, end, seg_id))

        paragraph_text = " ".join(parts)
        for start_char, end_char, sentence in _split_sentences_with_offsets(paragraph_text):
            overlapping = [
                span for span in spans
                if span[0] < end_char and span[1] > start_char
            ]
            if overlapping:
                start_time = _interpolate_span(overlapping[0], max(start_char, overlapping[0][0]))
                end_time = _interpolate_span(overlapping[-1], min(end_char, overlapping[-1][1]))
                segment_ids = [span[4] for span in overlapping]
            else:
                start_time = end_time = 0.0
                segment_ids = []

            sentences.append(
                {
                    "text": sentence,
                    "start": round(start_time, 3),
                    "end": round(max(start_time, end_time), 3),
                    "paragraph": paragraph_index,
                    "segment_ids": segment_ids,
                }
            )

    return {"version": 1, "duration": round(duration, 3), "sentences": sentences}


def render_formatted(document: dict[str, Any], *, max_line_length: int | None = None) -> str:
    """Render a sentence document as TXT.

    ``max_line_length`` is an export/display concern: long sentences are wrapped
    at word boundaries. The canonical sentences themselves are never split by
    commas, so translation and highlighting keep whole sentences.
    """
    by_paragraph: dict[int, list[str]] = {}
    for unit in document.get("sentences", []):
        text = str(unit.get("text") or "").strip()
        if not text:
            continue
        by_paragraph.setdefault(int(unit.get("paragraph", 0)), []).append(text)

    blocks: list[str] = []
    for paragraph_index in sorted(by_paragraph):
        lines: list[str] = []
        for text in by_paragraph[paragraph_index]:
            if max_line_length and len(text) > max_line_length:
                lines.extend(
                    textwrap.wrap(
                        text,
                        width=max_line_length,
                        break_long_words=False,
                        break_on_hyphens=False,
                    )
                    or [text]
                )
            else:
                lines.append(text)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


async def smart_format_document(
    document: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    """Split only sentences longer than the export limit, preserving word content."""
    formatted: list[dict[str, Any]] = []
    for unit in document.get("sentences", []):
        text = str(unit.get("text") or "")
        if len(text) <= MAX_LINE_LENGTH:
            formatted.append(unit)
            continue

        lines = await _split_long_sentence_with_groq(text, api_key)
        if not lines:
            formatted.append(unit)
            continue

        lengths = [max(len(line), 1) for line in lines]
        total = sum(lengths) or 1
        start = float(unit.get("start") or 0.0)
        end = float(unit.get("end") or start)
        cursor = start
        for line, length in zip(lines, lengths):
            share = (end - start) * length / total
            formatted.append(
                {
                    **unit,
                    "text": line,
                    "start": round(cursor, 3),
                    "end": round(cursor + share, 3),
                }
            )
            cursor += share

    return {**document, "sentences": formatted}


async def format_transcript(
    transcript: dict[str, Any],
    api_key: str | None = None,
    use_groq: bool = False,
) -> str:
    """Build the canonical sentence document and render it as TXT."""
    transcript = clean_transcript(transcript)
    if use_groq and not api_key:
        logging.warning("Smart formatting is enabled, but the user has no Groq API key — smart splitting will not be used.")

    document = build_sentence_document(transcript)
    if use_groq and api_key:
        document = await smart_format_document(document, api_key)
    return render_formatted(document)


def split_formatted_paragraphs(formatted_text: str) -> list[list[str]]:
    """Split formatted.txt into paragraphs of non-empty lines.

    A blank line closes a paragraph, which is exactly how the file is written
    (paragraphs joined with "\\n\\n"), so the page, the player and the translation
    all see the same sentence list.
    """
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for raw_line in formatted_text.splitlines():
        line = raw_line.strip()
        if line:
            current.append(line)
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    return paragraphs


def _word_timeline(segments: list[dict[str, Any]]) -> list[tuple[str, float, float]]:
    """Return (word, start, end) for every transcript word.

    A segment only carries its own start/end, so word times are interpolated
    inside the segment by character offset — the same approach the player uses.
    """
    timeline: list[tuple[str, float, float]] = []
    for seg in segments:
        text = " ".join((seg.get("text") or "").split())
        if not text:
            continue
        start = float(seg.get("start", 0.0) or 0.0)
        end = float(seg.get("end", start) or start)
        span = max(end - start, 0.0)
        total = len(text)
        for match in _WORD_RE.finditer(text):
            timeline.append(
                (
                    match.group(0).lower(),
                    start + (match.start() / total) * span,
                    start + (match.end() / total) * span,
                )
            )
    return timeline


def _match_line(
    words: list[str],
    timeline: list[tuple[str, float, float]],
    pointer: int,
) -> tuple[float, float, int] | None:
    """Locate the words of a formatted line in the word timeline.

    Returns the time range of the matched span and the new pointer, or None when
    no word could be found (the formatter rephrased the line).
    """
    start: float | None = None
    end: float | None = None
    for word in words:
        limit = min(len(timeline), pointer + ALIGN_LOOKAHEAD)
        found = next((i for i in range(pointer, limit) if timeline[i][0] == word), None)
        if found is None:
            continue
        if start is None:
            start = timeline[found][1]
        end = timeline[found][2]
        pointer = found + 1
    if start is None or end is None:
        return None
    return start, end, pointer


def _interpolate_missing_times(
    units: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> None:
    """Give rephrased lines a share of the free time between their neighbours."""
    total_end = max((float(seg.get("end", 0.0) or 0.0) for seg in segments), default=0.0)
    index = 0
    while index < len(units):
        if units[index]["start"] is not None:
            index += 1
            continue
        run_end = index
        while run_end < len(units) and units[run_end]["start"] is None:
            run_end += 1
        gap_start = units[index - 1]["end"] if index > 0 else 0.0
        gap_end = units[run_end]["start"] if run_end < len(units) else total_end
        gap_end = max(gap_start, gap_end)
        weights = [max(len(units[i]["text"]), 1) for i in range(index, run_end)]
        total_weight = sum(weights)
        cursor = gap_start
        for offset, unit_index in enumerate(range(index, run_end)):
            share = (gap_end - gap_start) * weights[offset] / total_weight
            units[unit_index]["start"] = cursor
            units[unit_index]["end"] = cursor + share
            cursor += share
        index = run_end


def build_sentence_units(
    transcript: dict[str, Any] | None,
    formatted_text: str,
) -> list[dict[str, Any]]:
    """Line up the lines of formatted.txt with the transcript timings.

    Every line of the formatted text becomes one unit, so the transcript page,
    the audio player and the translation work with the very same sentence list
    and a hover always pairs equivalent sentences. Lines the formatter rephrased
    get their times interpolated between their neighbours.
    """
    if not formatted_text.strip():
        return []

    segments = clean_transcript(transcript or {}).get("segments") or []
    timeline = _word_timeline(segments)

    units: list[dict[str, Any]] = []
    pointer = 0
    for paragraph_index, paragraph in enumerate(split_formatted_paragraphs(formatted_text)):
        for line in paragraph:
            matched = None
            if timeline:
                matched = _match_line([w.lower() for w in _WORD_RE.findall(line)], timeline, pointer)
            if matched:
                start, end, pointer = matched
                units.append({"text": line, "start": start, "end": end, "paragraph": paragraph_index})
            else:
                units.append({"text": line, "start": None, "end": None, "paragraph": paragraph_index})

    _interpolate_missing_times(units, segments)
    for unit in units:
        unit["start"] = round(float(unit["start"] or 0.0), 3)
        unit["end"] = round(float(unit["end"] or 0.0), 3)
    return units


def _translation_batches(
    sentences: list[str], batch_size: int | None
) -> list[list[str]]:
    if batch_size:
        return [
            sentences[start : start + batch_size]
            for start in range(0, len(sentences), batch_size)
        ]

    batches: list[list[str]] = []
    current: list[str] = []
    chars = 0
    for sentence in sentences:
        if current and chars + len(sentence) + 2 > settings.translation_batch_chars:
            batches.append(current)
            current = []
            chars = 0
        current.append(sentence)
        chars += len(sentence) + 2
    if current:
        batches.append(current)
    return batches


def _parse_translation_content(content: str, expected: int) -> list[str]:
    """Prefer the requested JSON array, but accept plain lines from older models."""
    stripped = content.strip()
    if stripped.startswith(("{", "[")):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                data = data.get("translations")
            if isinstance(data, list):
                return [str(item).strip() for item in data]
        except (TypeError, ValueError):
            pass

    translated: list[str] = []
    for line in content.splitlines():
        value = line.strip()
        if not value:
            continue
        value = re.sub(r"^\d+\.\s*", "", value)
        value = re.sub(r"^[-•]\s*", "", value)
        translated.append(value)
    return translated


async def _translate_batch(
    batch: list[str],
    target_lang: str,
    target_name: str,
    api_key: str,
) -> list[str]:
    batch_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(batch))
    prompt = (
        "You are a professional translator. Translate the following numbered "
        f"sentences into {target_name} ({target_lang}).\n"
        "Rules:\n"
        "1. Return exactly one translation per input line.\n"
        "2. Keep the same line order — line N of the output must translate line N of the input.\n"
        "3. Do not add, remove, or merge sentences.\n"
        "4. Do not include the numbering in the output.\n"
        "5. Do not add any commentary, explanations, or extra text.\n"
        "6. Preserve names, numbers, and proper nouns as-is.\n"
        f"7. Translate naturally into {target_name}.\n\n"
        f"Return a JSON object with one key, \"translations\", containing exactly "
        f"{len(batch)} strings in the same order.\n\n"
        "Input:\n"
        f"{batch_text}\n\n"
        "Output:\n"
    )

    payload = {
        "model": settings.groq_chat_model,
        "messages": [
            {"role": "system", "content": "You are a helpful translator."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": max(400, len(batch_text) // 2 + 300),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    translated: list[str] = []
    for attempt in range(2):
        try:
            response = await post_with_retries(
                GROQ_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise TranslationError(f"Translation request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise TranslationError(f"Groq rejected the API key (HTTP {response.status_code})")
        if response.status_code == 429:
            raise TranslationError("Groq rate limit reached. Try again later.")
        if response.status_code >= 400:
            raise TranslationError(f"Groq API error {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, TypeError, ValueError) as exc:
            raise TranslationError(f"Unexpected Groq response: {exc}") from exc

        translated = _parse_translation_content(content, len(batch))
        if len(translated) == len(batch):
            return translated
        logging.warning(
            "Translation batch returned %d lines for %d sentences (attempt %d)",
            len(translated),
            len(batch),
            attempt + 1,
        )

    raise TranslationError(
        f"Translation returned {len(translated)} lines for {len(batch)} sentences"
    )


async def translate_sentences(
    sentences: list[str],
    target_lang: str,
    api_key: str | None,
    batch_size: int | None = None,
) -> list[str]:
    """Translate sentences in parallel character-budgeted batches."""
    if not api_key:
        raise TranslationError("No API key available for translation")

    if not sentences:
        return []

    target_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    batches = _translation_batches(sentences, batch_size)
    semaphore = asyncio.Semaphore(max(1, settings.translation_concurrency))

    async def run(batch: list[str]) -> list[str]:
        async with semaphore:
            return await _translate_batch(batch, target_lang, target_name, api_key)

    translated_batches = await asyncio.gather(*(run(batch) for batch in batches))
    return [line for batch in translated_batches for line in batch]
