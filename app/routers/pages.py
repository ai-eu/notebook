import json
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import get_session_user
from app.models import Recording
from app.templates import templates
from app.utils import resolve_recording_path

router = APIRouter(tags=["pages"])


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_session_user(request, db)
    if not user:
        return RedirectResponse("/api/auth/login", status_code=303)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "user": user},
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
    transcript_path = resolve_recording_path(recording.folder_path) / "transcript.json"
    if transcript_path.exists():
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))

    return templates.TemplateResponse(
        "transcript.html",
        {
            "request": request,
            "user": user,
            "recording": recording,
            "transcript": transcript,
        },
    )
