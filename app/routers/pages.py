import json
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import get_session_user
from app.config import settings
from app.models import Recording
from app.services.storage.artifacts import (
    KIND_FORMATTED,
    KIND_SENTENCES,
    KIND_TRANSCRIPT,
    ensure_artifacts,
)
from app.templates import templates

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
        request,
        "index.html",
        {"user": user, "tts_enabled": settings.tts_enabled},
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

    artifacts = await ensure_artifacts(db, recording)

    transcript = None
    if KIND_TRANSCRIPT in artifacts:
        try:
            transcript = json.loads(artifacts[KIND_TRANSCRIPT])
        except (TypeError, ValueError):
            transcript = None

    sentence_units: list[dict] = []
    if KIND_SENTENCES in artifacts:
        try:
            document = json.loads(artifacts[KIND_SENTENCES])
            sentence_units = document.get("sentences") or []
        except (AttributeError, TypeError, ValueError):
            sentence_units = []

    return templates.TemplateResponse(
        request,
        "transcript.html",
        {
            "user": user,
            "recording": recording,
            "transcript": transcript,
            "sentence_units": sentence_units,
            "original_paragraphs": _group_units(sentence_units),
            "txt_ready": KIND_FORMATTED in artifacts or bool(sentence_units),
            "translation_enabled": settings.translation_enabled,
            "translation_languages": settings.translation_target_languages,
            # Short codes keep the dropdown unobtrusive (pt-PT shows as "pt").
            "translation_language_labels": {
                code: code.split("-")[0] for code in settings.translation_target_languages
            },
        },
    )
