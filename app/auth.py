import bcrypt
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import Request, HTTPException, status, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.config import settings
from app.models import User, Session as UserSession


def generate_key() -> str:
    return secrets.token_urlsafe(24)


def hash_key(key: str) -> str:
    return bcrypt.hashpw(key.encode(), bcrypt.gensalt(rounds=12)).decode()


def get_key_lookup_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def verify_key(key: str, hashed: str) -> bool:
    return bcrypt.checkpw(key.encode(), hashed.encode())


async def authenticate_user(db: AsyncSession, key: str) -> User | None:
    key = key.strip()
    lookup = get_key_lookup_hash(key)
    result = await db.execute(select(User).where(User.key_lookup_hash == lookup))
    user = result.scalar_one_or_none()
    if user and verify_key(key, user.key_hash):
        user.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
        return user
    return None


async def create_session(db: AsyncSession, user_id: int, ip: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.session_expire_days)
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
            UserSession.expires_at > datetime.now(timezone.utc),
        )
    )
    row = result.first()
    if not row:
        return None
    return row.User


async def delete_session(db: AsyncSession, session_id: str) -> None:
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(UserSession).where(UserSession.id == session_id))
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


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user
