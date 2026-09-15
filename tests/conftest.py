import asyncio
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

# Settings are read when `app.*` is first imported, so the test environment has to
# be configured before anything from the application is imported.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="dictaphone-tests-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP_DIR / 'test.db').as_posix()}"
os.environ["DATA_DIR"] = str(_TMP_DIR / "data")
os.environ["MOCK_TRANSCRIPTION"] = "true"
os.environ["SESSION_COOKIE_SECURE"] = "false"

os.chdir(Path(__file__).resolve().parent.parent)

from app.config import settings  # noqa: E402
from app.database import Base, engine, AsyncSessionLocal  # noqa: E402
from app.models import Recording, StoredFile, User  # noqa: E402
from app.services.storage import archive, telegram  # noqa: E402


class FakeResponse:
    """Stand-in for httpx.Response."""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeAsyncClient:
    """Minimal httpx.AsyncClient replacement that records the requests it gets."""

    def __init__(self, response: FakeResponse, **kwargs):
        self.response = response
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None, **kwargs):
        self.requests.append({"url": url, "headers": headers or {}})
        return self.response

    async def post(self, url, headers=None, json=None, data=None, files=None, **kwargs):
        self.requests.append({"url": url, "headers": headers or {}, "json": json, "data": data})
        return self.response


def _multipart_fields(request: httpx.Request) -> dict[str, bytes]:
    content_type = request.headers.get("content-type", "")
    if "boundary=" not in content_type:
        return {}
    boundary = content_type.split("boundary=", 1)[1].strip().encode()
    fields: dict[str, bytes] = {}
    for part in request.content.split(b"--" + boundary):
        headers, separator, body = part.partition(b"\r\n\r\n")
        if not separator or b'name="' not in headers:
            continue
        name = headers.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        fields[name] = body[:-2] if body.endswith(b"\r\n") else body
    return fields


def _form_fields(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


class FakeBotApi:
    """In-memory stand-in for the Telegram Bot API."""

    def __init__(self):
        self.calls: list[str] = []
        self.files: dict[str, bytes] = {}
        self.file_paths: dict[str, str] = {}
        self.messages: dict[int, dict] = {}
        self.contents: dict[int, bytes] = {}
        self.updates: list[dict] = []
        # method -> queued (status, payload) answers returned before the real one
        self.failures: dict[str, list[tuple[int, dict]]] = {}
        self.pauses = None
        self._next_message_id = 100

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/file/bot" in path:
            self.calls.append("download")
            file_path = path.split("/file/bot", 1)[1].split("/", 1)[1]
            return httpx.Response(200, content=self.files.get(self.file_paths.get(file_path, ""), b""))

        method = path.rsplit("/", 1)[-1]
        self.calls.append(method)
        queued = self.failures.get(method)
        if queued:
            status, payload = queued.pop(0)
            return httpx.Response(status, json=payload)
        return self._dispatch(method, request)

    def _dispatch(self, method: str, request: httpx.Request) -> httpx.Response:
        if method == "getMe":
            return self._ok({"id": 42, "username": "dictaphone_bot"})
        if method == "getUpdates":
            return self._ok(self.updates)
        if method == "sendDocument":
            fields = _multipart_fields(request)
            return self._ok(self._store(int(fields["chat_id"]), fields["document"], fields["caption"].decode()))
        if method == "copyMessage":
            fields = _form_fields(request)
            message_id = int(fields["message_id"])
            if message_id not in self.messages:
                return self._error(400, "Bad Request: message to copy not found")
            copy = self._store(int(fields["chat_id"]), self.contents[message_id], self.messages[message_id]["caption"])
            return self._ok(copy)
        if method == "getFile":
            fields = _form_fields(request)
            file_id = fields.get("file_id", "")
            if file_id not in self.files:
                return self._error(400, "Bad Request: file is not found")
            return self._ok({"file_id": file_id, "file_path": f"documents/{file_id}"})
        if method == "deleteMessage":
            fields = _form_fields(request)
            message_id = int(fields["message_id"])
            if message_id not in self.messages:
                return self._error(400, "Bad Request: message to delete not found")
            self.messages.pop(message_id, None)
            self.contents.pop(message_id, None)
            return self._ok(True)
        return self._error(404, f"unknown method {method}")

    def _store(self, chat_id: int, content: bytes, caption: str) -> dict:
        self._next_message_id += 1
        file_id = f"file-{len(self.files) + 1}"
        self.files[file_id] = content
        self.file_paths[f"documents/{file_id}"] = file_id
        message = {
            "message_id": self._next_message_id,
            "chat": {"id": chat_id, "type": "channel"},
            "caption": caption,
            "document": {"file_id": file_id, "file_unique_id": f"u-{file_id}", "file_size": len(content)},
        }
        self.messages[self._next_message_id] = message
        self.contents[self._next_message_id] = content
        return message

    def expire(self, file_id: str) -> None:
        """Simulate a file_id that Telegram no longer accepts: the message keeps the file."""
        self.files.pop(file_id, None)

    def _ok(self, result) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})

    def _error(self, status: int, description: str) -> httpx.Response:
        return httpx.Response(status, json={"ok": False, "description": description})


class Pauses:
    def __init__(self):
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


@pytest.fixture
def bot_api(monkeypatch):
    """A fake Telegram Bot API wired into the storage layer."""
    fake = FakeBotApi()
    pauses = Pauses()
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:TEST")
    monkeypatch.setattr(settings, "telegram_chat_id", "-1001234567890")
    monkeypatch.setattr(settings, "telegram_send_interval_seconds", 0)
    monkeypatch.setattr(telegram, "_new_client", lambda timeout=None: httpx.AsyncClient(transport=fake.transport()))
    monkeypatch.setattr(telegram, "_pause", pauses)
    fake.pauses = pauses
    return fake


def make_recording(monkeypatch, tmp_path, *, audio: bytes, name: str = "rec-1", with_transcript: bool = True) -> Path:
    """Create a recording folder plus its database row and point the archive at it."""
    folder = tmp_path / name
    folder.mkdir(parents=True)
    (folder / "audio.mp3").write_bytes(audio)
    if with_transcript:
        (folder / "transcript.json").write_text('{"text": "hi"}', encoding="utf-8")

    async def _create():
        async with AsyncSessionLocal() as db:
            user = User(groq_key=f"gsk_{name}", label="Alice")
            db.add(user)
            await db.commit()
            db.add(
                Recording(
                    user_id=user.id,
                    recording_id=name,
                    folder_path=name,
                    original_filename="lecture.m4a",
                    status="done",
                )
            )
            await db.commit()

    asyncio.run(_create())
    monkeypatch.setattr(archive, "resolve_recording_path", lambda path: tmp_path / Path(path).name)
    return folder


async def load_recording(recording_id: str) -> Recording:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(select(Recording).where(Recording.recording_id == recording_id))
        ).scalar_one()


async def load_files(recording_id: str) -> list[StoredFile]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(StoredFile)
                    .where(StoredFile.recording_id == recording_id)
                    .order_by(StoredFile.kind, StoredFile.idx)
                )
            ).scalars()
        )


@pytest.fixture(autouse=True)
def reset_db():
    """Recreate the schema before each test and drop pooled connections after it."""
    import shutil

    from app import auth
    from app.services.storage import cache

    auth._login_failures.clear()
    cache._locks.clear()
    # DATA_DIR holds the recordings and the audio cache: neither may leak between tests.
    shutil.rmtree(settings.data_dir_absolute, ignore_errors=True)

    async def recreate():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(recreate())
    yield
    asyncio.run(engine.dispose())
