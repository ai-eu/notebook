from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Float, Text, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


def now_utc():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    key_hash = Column(String(255), nullable=False)
    key_lookup_hash = Column(String(64), unique=True, index=True, nullable=False)
    label = Column(String(255), nullable=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=now_utc)
    last_seen_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    recordings = relationship("Recording", back_populates="user", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    id = Column(String(128), primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=now_utc)
    expires_at = Column(DateTime, nullable=False)
    ip_address = Column(String(64), nullable=True)

    user = relationship("User", back_populates="sessions")


class Recording(Base):
    __tablename__ = "recordings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    recording_id = Column(String(64), unique=True, index=True, nullable=False)
    folder_path = Column(String(500), nullable=False)
    original_filename = Column(String(255), nullable=True)
    duration = Column(Float, nullable=True)
    status = Column(String(20), default="pending")  # pending/processing/done/error
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    user = relationship("User", back_populates="recordings")



