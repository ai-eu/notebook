from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Float, Text, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


def now_utc():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    groq_key = Column(String(128), unique=True, index=True, nullable=False)
    label = Column(String(255), nullable=True)
    key_valid = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now_utc)
    last_seen_at = Column(DateTime, default=now_utc, onupdate=now_utc)
    last_verified_at = Column(DateTime, nullable=True)

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
    # local — only on this server, partial — some parts are in the channel, tg — fully archived
    storage_state = Column(String(20), default="local")
    archived_at = Column(DateTime, nullable=True)
    # Set by `tg-restore`: the local copy was asked for, so the cleanup must keep it
    keep_local = Column(Boolean, default=False)
    # User-defined labels separated by spaces, and a free-form note shown above the transcript
    tags = Column(String(500), nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=now_utc)
    updated_at = Column(DateTime, default=now_utc, onupdate=now_utc)

    user = relationship("User", back_populates="recordings")
    files = relationship(
        "StoredFile",
        back_populates="recording",
        cascade="all, delete-orphan",
        order_by="StoredFile.idx",
    )


class StoredFile(Base):
    """A copy of a recording file that lives in the Telegram channel."""

    __tablename__ = "stored_files"

    id = Column(Integer, primary_key=True, index=True)
    recording_id = Column(
        String(64),
        ForeignKey("recordings.recording_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind = Column(String(20), nullable=False)  # audio / transcript / formatted
    idx = Column(Integer, default=0)  # chunk index for audio
    local_path = Column(String(500), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=True)
    tg_chat_id = Column(String(64), nullable=True)
    tg_message_id = Column(Integer, nullable=True)
    tg_file_id = Column(Text, nullable=True)
    tg_file_unique_id = Column(String(128), nullable=True)
    uploaded_at = Column(DateTime, default=now_utc)

    recording = relationship("Recording", back_populates="files")

    __table_args__ = (UniqueConstraint("recording_id", "kind", "idx", name="uq_stored_file"),)



