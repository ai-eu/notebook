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
    get_auth_cookie_options,
    delete_session,
    login_rate_limited,
    login_with_groq_key,
    record_login_failure,
    redeem_device_invite,
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
    key: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ip = client_ip(request)
    if login_rate_limited(ip):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many failed attempts. Try again in a minute."},
            status_code=429,
        )

    user, error = await login_with_groq_key(db, key)
    if not user:
        record_login_failure(ip)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": error},
            status_code=401,
        )

    clear_login_failures(ip)
    session_id = await create_session(db, user.id, ip=ip)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(settings.session_cookie_name, session_id, **get_auth_cookie_options())
    return response


@router.get("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    session_id = request.cookies.get(settings.session_cookie_name)
    if session_id:
        await delete_session(db, session_id)
    response = RedirectResponse("/api/auth/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name)
    return response


@router.get("/device/link")
async def device_link(request: Request, user: User = Depends(require_user), db: AsyncSession = Depends(get_db)):
    """Return a one-time login URL for another device (e.g. to show as a QR code)."""
    token = await create_device_invite(db, user.id)
    base = str(request.base_url).rstrip("/")
    return {"url": f"{base}/api/auth/device/redeem/{token}", "ttl_seconds": DEVICE_INVITE_TTL_SECONDS}


@router.get("/device/redeem/{token}")
async def device_redeem(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Consume a one-time invite link and log the device in."""
    ip = client_ip(request)
    if login_rate_limited(ip):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many failed attempts. Try again in a minute."},
            status_code=429,
        )
    user = await redeem_device_invite(db, token)
    if user is None:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "This link is invalid, already used, or expired. Log in with your API key instead."},
            status_code=410,
        )
    clear_login_failures(ip)
    session_id = await create_session(db, user.id, ip=ip)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(settings.session_cookie_name, session_id, **get_auth_cookie_options())
    return response
