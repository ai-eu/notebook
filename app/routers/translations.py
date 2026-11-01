from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_user
from app.config import settings
from app.database import get_db
from app.models import Recording, User
from app.services import translation_jobs
from app.services.formatter import LANGUAGE_NAMES
from app.services.groq import resolve_groq_key
from app.services.storage.archive import (
    enqueue_artifact_cleanup,
    process_remote_cleanup,
)
from app.services.storage.artifacts import (
    KIND_SENTENCES,
    delete_artifact,
    ensure_artifacts,
    load_sentences,
    load_translation,
    text_sha256,
    translation_kind,
)
from app.utils import resolve_recording_path

router = APIRouter(prefix="/api/translate", tags=["translate"])


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
    if not api_key and not settings.gemini_api_key:
        raise HTTPException(
            status_code=401,
            detail="No Groq API key available for this user",
        )

    # A finished attempt reports its failure once; the next request starts over.
    failure = translation_jobs.pop_error(recording.recording_id, lang)
    if failure is not None:
        raise HTTPException(status_code=failure[0], detail=failure[1])

    job = translation_jobs.start_job(
        recording_id=recording.recording_id,
        folder_path=recording.folder_path,
        lang=lang,
        sentences=sentences,
        paragraphs=paragraphs,
        source_sha=source_sha,
        api_key=api_key,
    )
    # A long transcript takes minutes because of the provider rate limits, so the page
    # polls this endpoint until the translation artifact exists.
    return JSONResponse(translation_jobs.progress_payload(job), status_code=202)


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
    translation_jobs.cancel_job(recording.recording_id, lang)
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
