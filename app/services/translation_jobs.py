"""Background translation jobs.

Groq allows about 1000 output tokens per minute for the chat model, so a long
transcript needs minutes and cannot be served inside one HTTP request: nginx and the
CDN in front of it cut requests after a few dozen seconds. The API therefore starts a
job and the page polls its progress, the same way transcription reports progress.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.database import AsyncSessionLocal
from app.services.formatter import (
    TranslationError,
    TranslationRateLimitError,
    translate_sentences,
)
from app.services.jobs import job_runner
from app.services.storage.artifacts import (
    SCHEMA_VERSION,
    dump_json,
    put_artifact,
    translation_kind,
)
from app.utils import resolve_recording_path

TRANSLATION_ARTIFACT_VERSION = SCHEMA_VERSION


@dataclass
class TranslationJob:
    """Progress of one (recording, language) translation."""

    recording_id: str
    lang: str
    total: int
    done: int = 0
    error: str | None = None
    error_status: int = 502
    task: asyncio.Task | None = None


_jobs: dict[tuple[str, str], TranslationJob] = {}


def _key(recording_id: str, lang: str) -> tuple[str, str]:
    return (recording_id, lang)


def get_job(recording_id: str, lang: str) -> TranslationJob | None:
    job = _jobs.get(_key(recording_id, lang))
    if job is None or job.error is not None:
        return None
    return job


def progress_payload(job: TranslationJob) -> dict[str, Any]:
    return {"status": "running", "done": job.done, "total": job.total}


def pop_error(recording_id: str, lang: str) -> tuple[int, str] | None:
    """Return the failure once and forget it, so the next request starts over."""
    job = _jobs.get(_key(recording_id, lang))
    if job is None or job.error is None:
        return None
    _jobs.pop(_key(recording_id, lang), None)
    return job.error_status, job.error


def cancel_job(recording_id: str, lang: str) -> None:
    job = _jobs.pop(_key(recording_id, lang), None)
    if job is not None and job.task is not None:
        job.task.cancel()


def start_job(
    *,
    recording_id: str,
    folder_path: str,
    lang: str,
    sentences: list[str],
    paragraphs: list[list[str]],
    source_sha: str,
    api_key: str | None,
) -> TranslationJob:
    """Start the translation unless one is already running for this language."""
    job = get_job(recording_id, lang)
    if job is not None:
        return job

    job = TranslationJob(recording_id=recording_id, lang=lang, total=len(sentences))
    _jobs[_key(recording_id, lang)] = job

    async def run() -> None:
        try:
            translated = await translate_sentences(
                sentences,
                lang,
                api_key,
                progress=lambda done, total: _set_progress(job, done, total),
            )
            await _store(recording_id, folder_path, lang, translated, source_sha, paragraphs)
        except asyncio.CancelledError:
            _jobs.pop(_key(recording_id, lang), None)
            raise
        except TranslationRateLimitError as exc:
            job.error, job.error_status = str(exc), 429
        except TranslationError as exc:
            job.error, job.error_status = str(exc), 502
        except Exception as exc:
            job.error, job.error_status = f"Translation failed: {exc}", 502
            logging.exception("Translation job for %s (%s) failed", recording_id, lang)
        else:
            _jobs.pop(_key(recording_id, lang), None)
            return
        logging.warning(
            "Translation failed for %s (%s, %d sentences): %s",
            recording_id,
            lang,
            len(sentences),
            job.error,
        )

    job.task = job_runner.submit(run)
    return job


def _set_progress(job: TranslationJob, done: int, total: int) -> None:
    job.done = done
    job.total = total


def join_paragraphs(sentences: list[str], paragraphs: list[list[str]]) -> str:
    """Write translated sentences back with the paragraph breaks of the source."""
    blocks: list[str] = []
    offset = 0
    for paragraph in paragraphs:
        blocks.append("\n".join(sentences[offset : offset + len(paragraph)]))
        offset += len(paragraph)
    return "\n\n".join(blocks) + "\n"


async def _store(
    recording_id: str,
    folder_path: str,
    lang: str,
    translated: list[str],
    source_sha: str,
    paragraphs: list[list[str]],
) -> None:
    content = dump_json(
        {
            "version": TRANSLATION_ARTIFACT_VERSION,
            "source_sha256": source_sha,
            "sentences": translated,
        }
    )
    async with AsyncSessionLocal() as db:
        await put_artifact(db, recording_id, translation_kind(lang), content)

    # Keep the legacy TXT around for export/archive and old clients.
    try:
        cache_path = resolve_recording_path(folder_path) / f"formatted-{lang}.txt"
        cache_path.write_text(join_paragraphs(translated, paragraphs), encoding="utf-8")
    except OSError:
        pass
