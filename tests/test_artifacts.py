import asyncio

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.main import app
from app.models import Recording, RecordingArtifact, User
from app.services.storage.artifacts import (
    KIND_FORMATTED,
    KIND_SENTENCES,
    delete_artifact,
    get_content,
    put_artifact,
    save_formatted,
    save_sentences,
    text_sha256,
)

KEY = "gsk_" + "a" * 52


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _document() -> dict:
    return {
        "version": 1,
        "duration": 5.0,
        "sentences": [
            {
                "text": "Hello world.",
                "start": 0.0,
                "end": 1.0,
                "paragraph": 0,
                "segment_ids": [],
            },
            {
                "text": "This is a test.",
                "start": 2.0,
                "end": 3.0,
                "paragraph": 1,
                "segment_ids": [],
            },
        ],
    }


def test_artifact_round_trip_updates_in_place():
    async def scenario():
        async with AsyncSessionLocal() as db:
            user = User(groq_key=KEY, label="Alice")
            db.add(user)
            await db.commit()
            recording = Recording(
                user_id=user.id,
                recording_id="rec-artifact",
                folder_path="1/rec-artifact",
                original_filename="note.m4a",
                status="done",
            )
            db.add(recording)
            await db.commit()

            await put_artifact(db, "rec-artifact", KIND_FORMATTED, "hello")
            assert await get_content(db, "rec-artifact", KIND_FORMATTED) == "hello"
            row = (
                await db.execute(
                    select(RecordingArtifact).where(
                        RecordingArtifact.recording_id == "rec-artifact"
                    )
                )
            ).scalar_one()
            assert row.sha256 == text_sha256("hello")

            await put_artifact(db, "rec-artifact", KIND_FORMATTED, "bye")
            assert await get_content(db, "rec-artifact", KIND_FORMATTED) == "bye"
            count = await db.scalar(
                select(func.count()).select_from(RecordingArtifact)
            )
            assert count == 1

            assert await delete_artifact(db, "rec-artifact", KIND_FORMATTED) is True
            assert await get_content(db, "rec-artifact", KIND_FORMATTED) is None

            await put_artifact(db, "rec-artifact", KIND_SENTENCES, "{}")
            await db.delete(recording)
            await db.commit()
            count = await db.scalar(
                select(func.count()).select_from(RecordingArtifact)
            )
            assert count == 0

    asyncio.run(scenario())


def test_transcript_page_reads_artifacts_without_local_files():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            async with AsyncSessionLocal() as db:
                user_id = (await db.execute(select(User))).scalars().first().id
                db.add(
                    Recording(
                        user_id=user_id,
                        recording_id="rec-db",
                        folder_path="1/rec-db",
                        original_filename="note.m4a",
                        status="done",
                    )
                )
                await db.commit()

            async with AsyncSessionLocal() as db:
                await save_sentences(db, "rec-db", _document())
                await save_formatted(db, "rec-db", "Hello world.\n\nThis is a test.")

            page = await client.get("/t/rec-db")

            assert page.status_code == 200
            assert page.text.count('class="sentence sentence-line"') == 2
            assert 'data-start="0.000"' in page.text
            assert 'data-start="2.000"' in page.text
            # No text files exist for this recording.
            assert not (settings.data_dir_absolute / str(user_id) / "rec-db").exists()

    asyncio.run(scenario())


def test_download_uses_the_formatted_artifact():
    async def scenario():
        async with _client() as client:
            await client.post("/api/auth/login", data={"key": KEY})
            async with AsyncSessionLocal() as db:
                user_id = (await db.execute(select(User))).scalars().first().id
                db.add(
                    Recording(
                        user_id=user_id,
                        recording_id="rec-download",
                        folder_path="1/rec-download",
                        original_filename="lecture.m4a",
                        status="done",
                    )
                )
                await db.commit()

            async with AsyncSessionLocal() as db:
                await save_sentences(db, "rec-download", _document())
                await save_formatted(db, "rec-download", "Hello world.\n\nThis is a test.")

            response = await client.get("/api/recordings/rec-download/download")

            assert response.status_code == 200
            assert response.text == "Hello world.\n\nThis is a test."
            assert 'filename="lecture.txt"' in response.headers["content-disposition"]

            # refresh re-renders from the canonical sentences, ignoring a stale TXT.
            async with AsyncSessionLocal() as db:
                await save_formatted(db, "rec-download", "stale text")
            refreshed = await client.get(
                "/api/recordings/rec-download/download?refresh=true"
            )
            assert refreshed.status_code == 200
            assert refreshed.text == "Hello world.\n\nThis is a test."

    asyncio.run(scenario())
