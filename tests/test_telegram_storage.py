import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Recording, StoredFile, User
from app.services.storage import archive, telegram

KEY = "gsk_" + "a" * 52
CHAT_ID = "-1001234567890"


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
    from urllib.parse import parse_qs

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
            self.messages.pop(int(fields["message_id"]), None)
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
    fake = FakeBotApi()
    pauses = Pauses()
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:TEST")
    monkeypatch.setattr(settings, "telegram_chat_id", CHAT_ID)
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


def test_caption_round_trip():
    caption = archive.build_caption("rec-1", "audio", 1, 3, 12345, "ab" * 32)
    parsed = archive.parse_caption(caption)
    assert parsed == {"rid": "rec-1", "kind": "audio", "part": "2/3", "size": "12345", "sha": "ab" * 32}


def test_small_audio_is_uploaded_as_a_single_part(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"x" * 1024)

    async def no_ffprobe(path):
        raise AssertionError("small files must not be probed")

    monkeypatch.setattr(archive, "get_media_info", no_ffprobe)
    parts = asyncio.run(archive.plan_parts(folder, folder / "audio.mp3"))
    assert parts == [folder / "audio.mp3"]


def test_large_audio_is_split_below_the_telegram_limit(monkeypatch, tmp_path, bot_api):
    limit = settings.telegram_chunk_mb * 1024 * 1024
    folder = make_recording(monkeypatch, tmp_path, audio=b"x" * (limit + 1))
    captured = {}

    async def fake_info(path):
        return {}

    async def fake_split(path, output_dir, minutes, passthrough=False):
        captured["minutes"] = minutes
        captured["passthrough"] = passthrough
        output_dir.mkdir(parents=True, exist_ok=True)
        chunks = []
        for idx, size in enumerate((limit, 1024)):
            chunk = output_dir / f"chunk_{idx:03d}.mp3"
            chunk.write_bytes(b"y" * size)
            chunks.append(chunk)
        return chunks

    monkeypatch.setattr(archive, "get_media_info", fake_info)
    monkeypatch.setattr(archive, "get_audio_bitrate_kbps", lambda info: 48)
    monkeypatch.setattr(archive, "split_audio", fake_split)

    parts = asyncio.run(archive.plan_parts(folder, folder / "audio.mp3"))
    assert len(parts) == 2
    assert all(part.stat().st_size <= limit for part in parts)
    # 48 kbps for 19 MiB is 55 minutes, capped by CHUNK_MAX_MINUTES
    assert captured["minutes"] == settings.chunk_max_minutes
    # a stream copy keeps the audio untouched
    assert captured["passthrough"] is True


def test_archive_uploads_audio_and_transcript(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    audio_sha = hashlib.sha256((folder / "audio.mp3").read_bytes()).hexdigest()

    result = asyncio.run(archive.archive_recording("rec-1"))
    assert result == {"recording_id": "rec-1", "parts": 1, "uploaded": 2}
    assert bot_api.calls == ["sendDocument", "sendDocument"]

    files = asyncio.run(load_files("rec-1"))
    assert [(row.kind, row.idx) for row in files] == [("audio", 0), ("transcript", 0)]

    audio_row = files[0]
    assert audio_row.sha256 == audio_sha
    assert audio_row.size_bytes == (folder / "audio.mp3").stat().st_size
    assert bot_api.files[audio_row.tg_file_id] == (folder / "audio.mp3").read_bytes()
    assert bot_api.messages[audio_row.tg_message_id]["caption"] == archive.build_caption(
        "rec-1", "audio", 0, 1, audio_row.size_bytes, audio_sha
    )

    recording = asyncio.run(load_recording("rec-1"))
    assert recording.storage_state == "tg"
    assert recording.archived_at is not None


def test_archive_skips_files_that_are_already_uploaded(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    asyncio.run(archive.archive_recording("rec-1"))
    second = asyncio.run(archive.archive_recording("rec-1"))

    assert second["uploaded"] == 0
    assert bot_api.calls.count("sendDocument") == 2


def test_archive_retries_after_a_flood_wait(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, with_transcript=False)
    bot_api.failures["sendDocument"] = [
        (429, {"ok": False, "description": "Too Many Requests: retry after 1", "parameters": {"retry_after": 0}})
    ]

    asyncio.run(archive.archive_recording("rec-1"))

    assert bot_api.calls.count("sendDocument") == 2
    assert bot_api.pauses.seconds == [0.0]
    assert len(asyncio.run(load_files("rec-1"))) == 1


def test_fetch_audio_glues_the_parts_back_together(monkeypatch, tmp_path, bot_api):
    limit = settings.telegram_chunk_mb * 1024 * 1024
    folder = make_recording(monkeypatch, tmp_path, audio=b"x" * (limit + 1), with_transcript=False)

    async def fake_info(path):
        return {}

    async def fake_split(path, output_dir, minutes, passthrough=False):
        output_dir.mkdir(parents=True, exist_ok=True)
        chunks = []
        for idx, payload in enumerate((b"first-part" * 1000, b"second-part" * 500)):
            chunk = output_dir / f"chunk_{idx:03d}.mp3"
            chunk.write_bytes(payload)
            chunks.append(chunk)
        return chunks

    monkeypatch.setattr(archive, "get_media_info", fake_info)
    monkeypatch.setattr(archive, "get_audio_bitrate_kbps", lambda info: 48)
    monkeypatch.setattr(archive, "split_audio", fake_split)

    asyncio.run(archive.archive_recording("rec-1"))
    assert not (folder / archive.CHUNK_DIR_NAME).exists()

    restored = tmp_path / "restored.mp3"
    asyncio.run(archive.fetch_audio("rec-1", restored))
    assert restored.read_bytes() == b"first-part" * 1000 + b"second-part" * 500


def test_expired_file_id_is_refreshed_from_the_stored_message(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    asyncio.run(archive.archive_recording("rec-1"))
    row = asyncio.run(load_files("rec-1"))[0]
    bot_api.expire(row.tg_file_id)

    restored = tmp_path / "restored.mp3"
    asyncio.run(archive.fetch_audio("rec-1", restored))

    assert "copyMessage" in bot_api.calls
    assert restored.read_bytes() == (folder / "audio.mp3").read_bytes()
    refreshed = asyncio.run(load_files("rec-1"))[0]
    assert refreshed.tg_file_id != row.tg_file_id
    assert refreshed.tg_file_id in bot_api.files


def test_verify_reports_broken_files_when_repair_is_off(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, with_transcript=False)
    asyncio.run(archive.archive_recording("rec-1"))
    row = asyncio.run(load_files("rec-1"))[0]
    bot_api.expire(row.tg_file_id)

    problems = asyncio.run(archive.verify_recording("rec-1", repair=False))
    assert len(problems) == 1
    assert "audio[0]" in problems[0]
    assert "copyMessage" not in bot_api.calls

    assert asyncio.run(archive.verify_recording("rec-1")) == []
    assert "copyMessage" in bot_api.calls


def test_restore_keeps_an_existing_local_copy(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    asyncio.run(archive.archive_recording("rec-1"))

    path, downloaded = asyncio.run(archive.restore_recording("rec-1"))
    assert (path, downloaded) == (folder / "audio.mp3", False)

    (folder / "audio.mp3").unlink()
    path, downloaded = asyncio.run(archive.restore_recording("rec-1"))
    assert downloaded is True
    assert path.read_bytes() == b"lecture" * 100

    recording = asyncio.run(load_recording("rec-1"))
    # the channel copy is still the archive, but the local file has to survive a cleanup
    assert recording.storage_state == "tg"
    assert recording.keep_local is True


def test_fetch_audio_rejects_a_truncated_download(monkeypatch, tmp_path, bot_api):
    folder = make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, with_transcript=False)
    asyncio.run(archive.archive_recording("rec-1"))
    row = asyncio.run(load_files("rec-1"))[0]
    bot_api.files[row.tg_file_id] = bot_api.files[row.tg_file_id][:10]

    with pytest.raises(telegram.TelegramError, match="bytes instead of"):
        asyncio.run(archive.fetch_audio("rec-1", tmp_path / "restored.mp3"))
    assert not (tmp_path / "restored.mp3").exists()


def test_pending_recordings_lists_only_unarchived_ones(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, name="rec-1")
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100, name="rec-2")

    assert asyncio.run(archive.pending_recordings()) == ["rec-1", "rec-2"]
    asyncio.run(archive.archive_recording("rec-1"))
    assert asyncio.run(archive.pending_recordings()) == ["rec-2"]
    assert asyncio.run(archive.archived_recordings()) == ["rec-1"]

    summary = asyncio.run(archive.status_summary())
    assert summary["recordings"] == 2
    assert summary["states"]["tg"] == 1
    assert summary["files"] == 2


def test_discover_chats_reads_updates(monkeypatch, bot_api):
    bot_api.updates = [
        {"update_id": 1, "channel_post": {"message_id": 5, "chat": {"id": -1001234567890, "type": "channel", "title": "Lectures"}}},
        {"update_id": 2, "message": {"message_id": 6, "chat": {"id": 777, "type": "private", "first_name": "Andrii"}}},
    ]
    chats = asyncio.run(telegram.discover_chats())
    assert {chat["id"] for chat in chats} == {-1001234567890, 777}
    assert chats[0]["title"] == "Lectures"


def test_archive_refuses_to_run_without_configuration(monkeypatch, tmp_path, bot_api):
    make_recording(monkeypatch, tmp_path, audio=b"lecture" * 100)
    monkeypatch.setattr(settings, "telegram_enabled", False)
    with pytest.raises(telegram.TelegramNotConfigured):
        asyncio.run(archive.archive_recording("rec-1"))


def test_send_document_masks_the_token_in_status(monkeypatch, bot_api):
    assert telegram.mask_token("123456789:AAHsecretsecret") == "12345678...cret"
    assert telegram.mask_token(None) == "-"

