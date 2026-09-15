from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.auth import (
    clear_login_failures,
    client_ip,
    create_session,
    get_auth_cookie_options,
    delete_session,
    login_rate_limited,
    login_with_groq_key,
    record_login_failure,
)
from app.config import settings
from app.templates import templates

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
