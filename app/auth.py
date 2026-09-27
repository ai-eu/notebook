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


DEVICE_INVITE_TTL_SECONDS = 600  # codes are short-lived: 10 minutes


async def create_device_invite(db: AsyncSession, user_id: int) -> str:
    """Generate a one-time 6-digit code that a new device can redeem instead of the key."""
    from app.models import DeviceInvite

    # Housekeeping first: drop expired/used codes so the table stays tiny.
    await revoke_device_invites(db, user_id)

    # Retry on the (astronomically unlikely) unique-code collision.
    for _ in range(5):
        code = f"{secrets.randbelow(1_000_000):06d}"
        result = await db.execute(select(DeviceInvite).where(DeviceInvite.code == code))
        if result.scalar_one_or_none() is None:
            break
    else:
        return await create_device_invite(db, user_id)

    expires = _now().replace(tzinfo=None) + timedelta(seconds=DEVICE_INVITE_TTL_SECONDS)
    db.add(DeviceInvite(code=code, user_id=user_id, expires_at=expires))
    await db.commit()
    return code


async def redeem_device_invite(db: AsyncSession, code: str) -> User | None:
    """Consume a one-time code and return its user, or None if invalid."""
    from app.models import DeviceInvite

    code = (code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return None
    result = await db.execute(select(DeviceInvite).where(DeviceInvite.code == code))
    invite = result.scalar_one_or_none()
    # SQLite returns naive datetimes; compare in the same (UTC) convention.
    now = _now().replace(tzinfo=None)
    if invite is None or invite.used_at is not None or invite.expires_at < now:
        return None
    invite.used_at = now
    await db.commit()
    return await db.get(User, invite.user_id)


async def revoke_device_invites(db: AsyncSession, user_id: int) -> None:
    """Immediately invalidate any active invite codes of the user."""
    from app.models import DeviceInvite

    await db.execute(sa_delete(DeviceInvite).where(DeviceInvite.user_id == user_id))
    await db.commit()


async def list_user_sessions(db: AsyncSession, user_id: int) -> list[UserSession]:
    """Active (non-expired) sessions of the user, newest first."""
    result = await db.execute(
        select(UserSession)
        .where(UserSession.user_id == user_id, UserSession.expires_at > _now())
        .order_by(UserSession.created_at.desc())
    )
    return list(result.scalars())


async def delete_other_session(db: AsyncSession, user_id: int, session_id: str) -> bool:
    """Revoke one device session of this user. Returns True if it was deleted."""
    result = await db.execute(
        sa_delete(UserSession).where(
            UserSession.id == session_id, UserSession.user_id == user_id
        )
    )
    await db.commit()
    return result.rowcount > 0


def describe_user_agent(user_agent: str | None) -> str:
    """Human-readable device description parsed from a User-Agent header."""
    if not user_agent:
        return "Unknown device"
    ua = user_agent

    # Device kind / OS, roughly in order of specificity.
    os_name = None
    if "iPhone" in ua:
        os_name = "iPhone"
    elif "iPad" in ua:
        os_name = "iPad"
    elif "Android" in ua:
        os_name = "Android"
        # Most Android browsers put the device model in the first parentheses.
        if "(" in ua:
            part = ua.split("(", 1)[1].split(")", 1)[0]
            tokens = [t for t in part.split("; ") if "Android" not in t and "Linux" not in t
                      and not t.startswith("Build") and t.strip() not in ("wv", "K")]
            if tokens:
                os_name = f"Android ({tokens[0].strip()})"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "Mac"
    elif "CrOS" in ua:
        os_name = "ChromeOS"
    elif "Linux" in ua:
        os_name = "Linux"

    # Browser.
    browser = None
    for marker, name in [
        ("Edg/", "Edge"), ("OPR/", "Opera"), ("Chrome/", "Chrome"),
        ("Firefox/", "Firefox"), ("Safari/", "Safari"),
    ]:
        if marker in ua:
            browser = name
            break
    if browser is None and "iPhone" not in ua and "iPad" not in ua:
        return os_name or "Unknown device"
    if browser is None:
        return os_name or "Mobile browser"

    return f"{os_name or 'Unknown OS'} · {browser}" if os_name else browser


async def create_session(
    db: AsyncSession, user_id: int, ip: str | None = None, user_agent: str | None = None
) -> str:
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=settings.session_expire_days)
    db.add(UserSession(
        id=token,
        user_id=user_id,
        expires_at=expires,
        ip_address=ip,
        user_agent=(user_agent or "")[:300],
    ))
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


def get_auth_cookie_options(request: Request | None = None) -> dict:
    secure = settings.session_cookie_secure
    if request is not None:
        # A `Secure` cookie is silently dropped by browsers on plain HTTP, which
        # locks devices out when the app is served without TLS (e.g. LAN access).
        # Behind a trusted proxy nginx sets x-forwarded-proto; honour it.
        if settings.trust_proxy_headers:
            proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
            secure = secure and proto == "https"
        elif request.url.scheme != "https":
            secure = False
    return {
        "httponly": True,
        "secure": secure,
        "samesite": settings.session_cookie_samesite,
        "path": "/",
        "max_age": settings.session_expire_days * 86400,
    }


async def require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await get_session_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user
