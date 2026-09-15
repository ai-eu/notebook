import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from fastapi import Request, HTTPException, status, Depends
from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.config import settings
from app.models import User, Session as UserSession
from app.services.groq import is_groq_key_format, mask_key, validate_key


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Failed login attempts per client IP, used to slow down key guessing.
# In-memory: counters reset on restart and are not shared between processes.
_login_failures: dict[str, list[float]] = {}


def _recent_failures(ip: str) -> list[float]:
    now = time.monotonic()
    recent = [t for t in _login_failures.get(ip, []) if now - t < settings.login_rate_limit_window_seconds]
    if recent:
        _login_failures[ip] = recent
    else:
        _login_failures.pop(ip, None)
    return recent


def client_ip(request: Request) -> str | None:
    """Client address, honouring proxy headers only when they are explicitly trusted."""
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return request.client.host if request.client else None


def login_rate_limited(ip: str | None) -> bool:
    if not ip:
        return False
    return len(_recent_failures(ip)) >= settings.login_rate_limit_attempts


def record_login_failure(ip: str | None) -> None:
    if not ip:
        return
    _login_failures.setdefault(ip, []).append(time.monotonic())


def clear_login_failures(ip: str | None) -> None:
    if ip:
        _login_failures.pop(ip, None)


async def login_with_groq_key(db: AsyncSession, key: str) -> tuple[User | None, str | None]:
    """Sign in with a Groq API key, registering a new user when allowed.

    Returns (user, error) — exactly one of the two is set.
    """
    key = key.strip()
    if not is_groq_key_format(key):
        return None, "That does not look like a Groq API key (expected gsk_...)."

    result = await db.execute(select(User).where(User.groq_key == key))
    user = result.scalar_one_or_none()
    check_remote = settings.validate_groq_key_on_login and not settings.mock_transcription

    if user is None:
        if not settings.allow_self_registration:
            return None, "This key is not allowed. Ask the administrator to add it."
        if check_remote:
            # A brand new key must be proven valid before it creates an account.
            result_status = await validate_key(key)
            if result_status == "invalid":
                return None, "Groq rejected this API key."
            if result_status == "unreachable":
                return None, "Could not verify the key with Groq. Please try again."
        user = User(groq_key=key, label=mask_key(key), last_verified_at=_now())
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user, None

    # Known key: never lock a user out of their own transcripts, but flag a dead key
    # so the UI can ask for a new one.
    if check_remote:
        result_status = await validate_key(key)
        if result_status == "invalid":
            logging.warning("Groq rejected the stored key of user %s", user.id)
            user.key_valid = False
            await db.commit()
            return user, None
        if result_status in ("valid", "throttled"):
            user.key_valid = True
            user.last_verified_at = _now()

    user.last_seen_at = _now()
    await db.commit()
    return user, None


async def create_session(db: AsyncSession, user_id: int, ip: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=settings.session_expire_days)
    db.add(UserSession(id=token, user_id=user_id, expires_at=expires, ip_address=ip))
    await db.commit()
    return token


async def get_session_user(request: Request, db: AsyncSession) -> User | None:
    session_id = request.cookies.get(settings.session_cookie_name)
    if not session_id:
        return None
    result = await db.execute(
        select(UserSession, User)
        .join(User)
        .where(
            UserSession.id == session_id,
            UserSession.expires_at > _now(),
        )
    )
    row = result.first()
    if not row:
        return None
    return row.User


async def delete_session(db: AsyncSession, session_id: str) -> None:
    await db.execute(sa_delete(UserSession).where(UserSession.id == session_id))
    await db.commit()


async def delete_user_sessions(db: AsyncSession, user_id: int) -> None:
    await db.execute(sa_delete(UserSession).where(UserSession.user_id == user_id))
    await db.commit()


def get_auth_cookie_options() -> dict:
    return {
        "httponly": True,
        "secure": settings.session_cookie_secure,
        "samesite": settings.session_cookie_samesite,
        "path": "/",
        "max_age": settings.session_expire_days * 86400,
    }


async def require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await get_session_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user
