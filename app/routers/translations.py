import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_user
from app.config import settings
from app.database import get_db
from app.models import Recording, User
from app.services.formatter import LANGUAGE_NAMES, TranslationError, translate_sentences
from app.services.groq import resolve_groq_key
from app.services.storage.archive import (
    enqueue_artifact_cleanup,
    process_remote_cleanup,
)
from app.services.storage.artifacts import (
    KIND_SENTENCES,
    delete_artifact,
    dump_json,
    ensure_artifacts,
    load_sentences,
    load_translation,
    put_artifact,
    text_sha256,
    translation_kind,
)
from app.utils import resolve_recording_path

router = APIRouter(prefix="/api/translate", tags=["translate"])

# One translation request at a time per (recording, language). Concurrent readers
# otherwise pay for the same Groq call and race on the cache file.
_locks: dict[tuple[object, str, str], asyncio.Lock] = {}


def _translation_lock(recording_id: str, lang: str) -> asyncio.Lock:
    # Keyed by loop as well: tests create a fresh loop per scenario.
    key = (asyncio.get_running_loop(), recording_id, lang)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _paragraph_indexes(paragraphs: list[list[str]]) -> list[int]:
    """Paragraph number of every sentence of the flattened list."""
    return [index for index, paragraph in enumerate(paragraphs) for _ in paragraph]


def _split_paragraphs(document: dict) -> list[list[str]]:
    paragraphs: list[list[str]] = []
    for sentence in document.get("sentences", []):
        index = int(sentence.get("paragraph", 0))
        while len(paragraphs) <= index:
            paragraphs.append([])
        paragraphs[index].append(str(sentence.get("text") or ""))
    return paragraphs


def _translation_payload(
    recording_id: str,
    lang: str,
    sentences: list[str],
    paragraphs: list[list[str]],
    cached: bool,
) -> dict:
    return {
        "recording_id": recording_id,
        "language": lang,
        "language_name": LANGUAGE_NAMES.get(lang, lang),
        "sentences": sentences,
        "paragraphs": _paragraph_indexes(paragraphs),
        "cached": cached,
    }


def _join_paragraphs(sentences: list[str], paragraphs: list[list[str]]) -> str:
    """Write translated sentences back with the paragraph breaks of the source."""
    blocks: list[str] = []
    offset = 0
    for paragraph in paragraphs:
        blocks.append("\n".join(sentences[offset : offset + len(paragraph)]))
        offset += len(paragraph)
    return "\n\n".join(blocks) + "\n"


async def _recording_for_user(
    recording_id: str, user: User, db: AsyncSession
) -> Recording:
    result = await db.execute(
        select(Recording).where(
            Recording.recording_id == recording_id,
            Recording.user_id == user.id,
        )
    )
    recording = result.scalar_one_or_none()
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")
    return recording


@router.get("")
async def translation_languages(user: User = Depends(require_user)):
    """Return supported target languages and whether translation is enabled."""
    _ = user
    return {
        "enabled": settings.translation_enabled,
        "default": settings.translation_default_language,
        "languages": [
            {"code": code, "name": LANGUAGE_NAMES.get(code, code)}
            for code in settings.translation_target_languages
        ],
    }


@router.get("/{recording_id}")
async def get_translation(
    recording_id: str,
    lang: str = Query(default=settings.translation_default_language),
    refresh: bool = Query(False, description="Re-translate, ignoring cached translation"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Return translated sentences for a recording, cached as a SQLite artifact."""
    if not settings.translation_enabled:
        raise HTTPException(status_code=404, detail="Translation is disabled")

    if lang not in settings.translation_target_languages:
        raise HTTPException(status_code=400, detail=f"Unsupported language: {lang}")

    recording = await _recording_for_user(recording_id, user, db)
    artifacts = await ensure_artifacts(db, recording)
    document = await load_sentences(db, recording.recording_id)
    if not document or not document.get("sentences"):
        raise HTTPException(status_code=404, detail="No sentences to translate")

    paragraphs = _split_paragraphs(document)
    sentences = [line for paragraph in paragraphs for line in paragraph]
    if not sentences:
        raise HTTPException(status_code=404, detail="No sentences to translate")

    source_sha = text_sha256(artifacts[KIND_SENTENCES])
    cached_translation = await load_translation(db, recording.recording_id, lang)
    if (
        not refresh
        and cached_translation
        and cached_translation.get("source_sha256") == source_sha
        and len(cached_translation.get("sentences") or []) == len(sentences)
    ):
        return _translation_payload(
            recording_id,
            lang,
            cached_translation["sentences"],
            paragraphs,
            cached=True,
        )

    api_key = resolve_groq_key(user)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="No Groq API key available for this user",
        )

    lock = _translation_lock(recording.recording_id, lang)
    async with lock:
        # Another request may have completed the translation while we waited.
        cached_translation = await load_translation(db, recording.recording_id, lang)
        if (
            not refresh
            and cached_translation
            and cached_translation.get("source_sha256") == source_sha
            and len(cached_translation.get("sentences") or []) == len(sentences)
        ):
            return _translation_payload(
                recording_id,
                lang,
                cached_translation["sentences"],
                paragraphs,
                cached=True,
            )

        try:
            translated = await translate_sentences(sentences, lang, api_key)
        except TranslationError as exc:
            logging.warning(
                "Translation failed for %s (%s, %d sentences): %s",
                recording_id,
                lang,
                len(sentences),
                exc,
            )
            raise HTTPException(status_code=502, detail=str(exc))

        content = dump_json(
            {
                "version": 1,
                "source_sha256": source_sha,
                "sentences": translated,
            }
        )
        await put_artifact(
            db,
            recording.recording_id,
            translation_kind(lang),
            content,
        )

        # Keep the legacy TXT around for export/archive and old clients.
        try:
            cache_path = resolve_recording_path(recording.folder_path) / f"formatted-{lang}.txt"
            cache_path.write_text(
                _join_paragraphs(translated, paragraphs), encoding="utf-8"
            )
        except OSError:
            pass

    return _translation_payload(recording_id, lang, translated, paragraphs, cached=False)


@router.delete("/{recording_id}")
async def delete_translation(
    recording_id: str,
    lang: str = Query(default=settings.translation_default_language),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete the cached translation (forces re-translation on the next request)."""
    if not settings.translation_enabled:
        raise HTTPException(status_code=404, detail="Translation is disabled")
    if lang not in settings.translation_target_languages:
        raise HTTPException(status_code=400, detail=f"Unsupported language: {lang}")

    recording = await _recording_for_user(recording_id, user, db)
    await delete_artifact(db, recording.recording_id, translation_kind(lang))
    await enqueue_artifact_cleanup(recording.recording_id, translation_kind(lang))
    # Old archives used the "formatted-<lang>" kind.
    await enqueue_artifact_cleanup(recording.recording_id, f"formatted-{lang}")
    await process_remote_cleanup()

    cache_path = resolve_recording_path(recording.folder_path) / f"formatted-{lang}.txt"
    try:
        cache_path.unlink(missing_ok=True)
    except OSError:
        pass

    return {"deleted": True}
