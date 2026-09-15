from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


# Load .env relative to the repository root, not the server's cwd.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    groq_api_key: str
    database_url: str = "sqlite+aiosqlite:///app.db"
    data_dir: Path = Path("data")
    upload_max_size_mb: int = 500
    target_mp3_kbps: int = 48
    mp3_passthrough_kbps: int = 196
    target_chunk_mb: int = 22
    chunk_max_minutes: int = 40
    session_cookie_name: str = "session_id"
    session_expire_days: int = 30
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "lax"
    mock_transcription: bool = False
    smart_format: bool = False
    groq_chat_model: str = "openai/gpt-oss-20b"
    whisper_model: str = "whisper-large-v3"
    whisper_prompt: str = "Please transcribe with proper punctuation, sentence breaks, and paragraph breaks."

    @property
    def data_dir_absolute(self) -> Path:
        return (Path.cwd() / self.data_dir).resolve()

    @property
    def upload_max_size_bytes(self) -> int:
        return self.upload_max_size_mb * 1024 * 1024


settings = Settings()
