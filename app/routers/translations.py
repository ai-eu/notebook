from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_user
from app.config import settings
from app.database import get_db
from app.models import Recording, User
from app.services.formatter import LANGUAGE_NAMES, TranslationError, translate_sentences
from app.services.groq import resolve_groq_key
from app.utils import resolve_recording_path

router = APIRouter(prefix="/api/translate", tags=["translate"])


@router.get("")
async def translation_languages():
    """Return supported target languages and whether translation is enabled."""
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
    refresh: bool = Query(False, description="Re-translate, ignoring cached file"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the translated sentences for a recording.

    The result is cached in formatted-<lang>.txt next to formatted.txt.
    If the cache exists and refresh is false, it is returned directly.
    Otherwise the formatted text is translated via Groq and saved.
    """
    if not settings.translation_enabled:
        raise HTTPException(
            status_code=404,
            detail="Translation is disabled",
        )

    if lang not in settings.translation_target_languages:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language: {lang}",
        )

    result = await db.execute(
        select(Recording).where(
            Recording.recording_id == recording_id,
            Recording.user_id == user.id,
        )
    )
    recording = result.scalar_one_or_none()
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")

    folder = resolve_recording_path(recording.folder_path)
    formatted_path = folder / "formatted.txt"
    if not formatted_path.exists():
        raise HTTPException(status_code=404, detail="Transcript not formatted yet")

    cache_path = folder / f"formatted-{lang}.txt"

    if not refresh and cache_path.exists():
        text = cache_path.read_text(encoding="utf-8")
        sentences = [line for line in text.split("\n") if line.strip()]
        return {
            "recording_id": recording_id,
            "language": lang,
            "language_name": LANGUAGE_NAMES.get(lang, lang),
            "sentences": sentences,
            "cached": True,
        }

    # Read the formatted text and split into lines (sentences).
    text = formatted_path.read_text(encoding="utf-8")
    sentences = [line.strip() for line in text.split("\n") if line.strip()]
    if not sentences:
        raise HTTPException(status_code=status.HTTP_404, detail="No sentences to translate")

    api_key = resolve_groq_key(user)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="No Groq API key available for this user",
        )

    try:
        translated = await translate_sentences(sentences, lang, api_key)
    except TranslationError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Save to cache file.
    cache_path.write_text("\n".join(translated) + "\n", encoding="utf-8")

    return {
        "recording_id": recording_id,
        "language": lang,
        "language_name": LANGUAGE_NAMES.get(lang, lang),
        "sentences": translated,
        "cached": False,
    }


@router.delete("/{recording_id}")
async def delete_translation(
    recording_id: str,
    lang: str = Query(default=settings.translation_default_language),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete the cached translation file (forces re-translation on next request)."""
    if not settings.translation_enabled:
        raise HTTPException(
            status_code=404,
            detail="Translation is disabled",
        )

    result = await db.execute(
        select(Recording).where(
            Recording.recording_id == recording_id,
            Recording.user_id == user.id,
        )
    )
    recording = result.scalar_one_or_none()
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")

    folder = resolve_recording_path(recording.folder_path)
    cache_path = folder / f"formatted-{lang}.txt"
    if cache_path.exists():
        cache_path.unlink()

    return {"deleted": True}
