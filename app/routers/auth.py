from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import (
    DEVICE_INVITE_TTL_SECONDS,
    clear_login_failures,
    client_ip,
    create_device_invite,
    create_session,
    delete_other_session,
    get_auth_cookie_options,
    delete_session,
    list_user_sessions,
    describe_user_agent,
    login_rate_limited,
    login_with_groq_key,
    record_login_failure,
    redeem_device_invite,
    revoke_device_invites,
)
from app.config import settings
from app.templates import templates
from app.auth import require_user
from app.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/login", response_class=HTMLResponse, name="login")
async def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@router.post("/login")
async def login(
    request: Request,
    key: str = Form(None),
    code: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    ip = client_ip(request)
    if login_rate_limited(ip):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many failed attempts. Try again in a minute."},
            status_code=429,
        )

    # Two ways in: the Groq API key, or a one-time 6-digit code from another device.
    user = error = None
    if code and code.strip():
        user = await redeem_device_invite(db, code)
        if user is None:
            record_login_failure(ip)
            error = "Invalid, already used, or expired code. Generate a new one on a signed-in device."
    else:
        user, error = await login_with_groq_key(db, key or "")
    if not user:
        record_login_failure(ip)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": error},
            status_code=401,
        )

    clear_login_failures(ip)
    session_id = await create_session(db, user.id, ip=ip, user_agent=request.headers.get("user-agent"))
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(settings.session_cookie_name, session_id, **get_auth_cookie_options(request))
    return response


@router.get("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        await delete_session(db, session_id)
    response = RedirectResponse("/api/auth/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name)
    return response


@router.post("/device/code")
async def device_code(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Generate a one-time 6-digit code for another device. Any previous code is revoked."""
    code = await create_device_invite(db, user.id)
    return {"code": code, "ttl_seconds": DEVICE_INVITE_TTL_SECONDS}


@router.post("/device/revoke-code")
async def device_revoke_code(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Immediately invalidate the active code (e.g. when it leaked into a chat)."""
    await revoke_device_invites(db, user.id)
    return {"ok": True}


@router.get("/sessions")
async def sessions_list(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """List active device sessions of the current user, newest first."""
    current = request.cookies.get(settings.session_cookie_name)
    items = [
        {
            "id": s.id,
            "current": s.id == current,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "ip_address": s.ip_address,
            "device": describe_user_agent(s.user_agent),
        }
        for s in await list_user_sessions(db, user.id)
    ]
    return {"sessions": items}


@router.delete("/sessions/{session_id}")
async def session_delete(session_id: str, request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Revoke (log out) one device session of the current user."""
    deleted = await delete_other_session(db, user.id, session_id)
    if not deleted:
        return {"ok": False, "error": "No such active session."}
    return {"ok": True}
