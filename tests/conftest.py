import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

# Settings are read when `app.*` is first imported, so the test environment has to
# be configured before anything from the application is imported.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="dictaphone-tests-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP_DIR / 'test.db').as_posix()}"
os.environ["DATA_DIR"] = str(_TMP_DIR / "data")
os.environ["MOCK_TRANSCRIPTION"] = "true"
os.environ["SESSION_COOKIE_SECURE"] = "false"

os.chdir(Path(__file__).resolve().parent.parent)

from app.database import Base, engine  # noqa: E402


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


@pytest.fixture(autouse=True)
def reset_db():
    """Recreate the schema before each test and drop pooled connections after it."""
    from app import auth

    auth._login_failures.clear()

    async def recreate():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(recreate())
    yield
    asyncio.run(engine.dispose())
