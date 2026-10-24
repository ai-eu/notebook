import json
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import get_session_user
from app.config import settings
from app.models import Recording
from app.services.formatter import build_sentence_units, clean_transcript
from app.templates import templates
from app.utils import resolve_recording_path

router = APIRouter(tags=["pages"])


def _group_units(units: list[dict]) -> list[list[dict]]:
    """Group the sentence units by paragraph, numbering them for the page."""
    groups: list[list[dict]] = []
    for index, unit in enumerate(units):
        if not groups or unit["paragraph"] != units[index - 1]["paragraph"]:
            groups.append([])
        groups[-1].append({**unit, "index": index})
    return groups


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_session_user(request, db)
    if not user:
        return RedirectResponse("/api/auth/login", status_code=303)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "user": user, "tts_enabled": settings.tts_enabled},
    )


@router.get("/t/{recording_id}", response_class=HTMLResponse)
async def transcript_page(
    recording_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_session_user(request, db)
    if not user:
        return RedirectResponse("/api/auth/login", status_code=303)
    result = await db.execute(
        select(Recording).where(
            Recording.recording_id == recording_id,
            Recording.user_id == user.id,
        )
    )
    recording = result.scalar_one_or_none()
    if not recording:
        raise HTTPException(status_code=404, detail="Recording not found")

    transcript = None
    sentence_units: list[dict] = []
    transcript_path = resolve_recording_path(recording.folder_path) / "transcript.json"
    formatted_path = resolve_recording_path(recording.folder_path) / "formatted.txt"
    if transcript_path.exists():
        transcript = clean_transcript(json.loads(transcript_path.read_text(encoding="utf-8")))
    if formatted_path.exists():
        # The lines of formatted.txt are the single source of sentences: the page
        # renders them with timings and the translation translates the very same list.
        sentence_units = build_sentence_units(
            transcript, formatted_path.read_text(encoding="utf-8")
        )

    return templates.TemplateResponse(
        "transcript.html",
        {
            "request": request,
            "user": user,
            "recording": recording,
            "transcript": transcript,
            "sentence_units": sentence_units,
            "original_paragraphs": _group_units(sentence_units),
            "txt_ready": formatted_path.exists(),
            "translation_enabled": settings.translation_enabled,
            "translation_languages": settings.translation_target_languages,
            "translation_default_language": settings.translation_default_language,
            "translation_language_names": {
                code: name
                for code, name in {
                    "uk": "Українська",
                    "pt-PT": "Português",
                    "es": "Español",
                    "en": "English",
                }.items()
                if code in settings.translation_target_languages
            },
        },
    )
