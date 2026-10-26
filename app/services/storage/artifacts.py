import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Recording, RecordingArtifact
from app.services.formatter import (
    MAX_LINE_LENGTH,
    build_sentence_document,
    build_sentence_units,
    clean_transcript,
    render_formatted,
    split_formatted_paragraphs,
)
from app.utils import resolve_recording_path

KIND_TRANSCRIPT = "transcript"
KIND_SENTENCES = "sentences"
KIND_FORMATTED = "formatted"
SCHEMA_VERSION = 1


def translation_kind(lang: str) -> str:
    return f"translation:{lang}"


def text_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def get_artifact(
    db: AsyncSession, recording_id: str, kind: str
) -> RecordingArtifact | None:
    return (
        await db.execute(
            select(RecordingArtifact).where(
                RecordingArtifact.recording_id == recording_id,
                RecordingArtifact.kind == kind,
            )
        )
    ).scalar_one_or_none()


async def get_content(db: AsyncSession, recording_id: str, kind: str) -> str | None:
    artifact = await get_artifact(db, recording_id, kind)
    return artifact.content if artifact else None


async def get_artifacts(db: AsyncSession, recording_id: str) -> dict[str, str]:
    rows = (
        await db.execute(
            select(RecordingArtifact).where(RecordingArtifact.recording_id == recording_id)
        )
    ).scalars().all()
    return {row.kind: row.content for row in rows}


async def put_artifact(
    db: AsyncSession,
    recording_id: str,
    kind: str,
    content: str,
    *,
    version: int = SCHEMA_VERSION,
    commit: bool = True,
) -> RecordingArtifact:
    sha = text_sha256(content)
    artifact = await get_artifact(db, recording_id, kind)
    if artifact is None:
        artifact = RecordingArtifact(
            recording_id=recording_id,
            kind=kind,
            content=content,
            sha256=sha,
            version=version,
        )
        db.add(artifact)
    else:
        artifact.content = content
        artifact.sha256 = sha
        artifact.version = version
        artifact.updated_at = datetime.now(timezone.utc)

    if commit:
        await db.commit()
    return artifact


async def delete_artifact(
    db: AsyncSession, recording_id: str, kind: str, *, commit: bool = True
) -> bool:
    artifact = await get_artifact(db, recording_id, kind)
    if artifact is None:
        return False
    await db.delete(artifact)
    if commit:
        await db.commit()
    return True


def _parse_json(content: str | None) -> Any | None:
    if not content:
        return None
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        logging.warning("Stored artifact is not valid JSON")
        return None


async def load_transcript(db: AsyncSession, recording_id: str) -> dict[str, Any] | None:
    value = _parse_json(await get_content(db, recording_id, KIND_TRANSCRIPT))
    return value if isinstance(value, dict) else None


async def load_sentences(db: AsyncSession, recording_id: str) -> dict[str, Any] | None:
    value = _parse_json(await get_content(db, recording_id, KIND_SENTENCES))
    if isinstance(value, dict) and isinstance(value.get("sentences"), list):
        return value
    return None


async def load_translation(
    db: AsyncSession, recording_id: str, lang: str
) -> dict[str, Any] | None:
    value = _parse_json(await get_content(db, recording_id, translation_kind(lang)))
    if isinstance(value, dict) and isinstance(value.get("sentences"), list):
        return value
    return None


async def save_transcript(
    db: AsyncSession,
    recording_id: str,
    transcript: dict[str, Any],
    *,
    commit: bool = True,
) -> str:
    content = dump_json(transcript)
    await put_artifact(db, recording_id, KIND_TRANSCRIPT, content, commit=commit)
    return content


async def save_sentences(
    db: AsyncSession,
    recording_id: str,
    document: dict[str, Any],
    *,
    commit: bool = True,
) -> str:
    content = dump_json(document)
    await put_artifact(db, recording_id, KIND_SENTENCES, content, commit=commit)
    return content


async def save_formatted(
    db: AsyncSession,
    recording_id: str,
    text: str,
    *,
    commit: bool = True,
) -> str:
    await put_artifact(db, recording_id, KIND_FORMATTED, text, commit=commit)
    return text


async def ensure_artifacts(
    db: AsyncSession,
    recording: Recording,
    *,
    commit: bool = True,
) -> dict[str, str]:
    """Fill missing artifacts, importing legacy files when they are still present."""
    artifacts = await get_artifacts(db, recording.recording_id)
    folder = resolve_recording_path(recording.folder_path)
    changed = False

    if KIND_TRANSCRIPT not in artifacts:
        path = folder / "transcript.json"
        if path.exists():
            try:
                transcript = clean_transcript(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                artifacts[KIND_TRANSCRIPT] = dump_json(transcript)
                await put_artifact(
                    db,
                    recording.recording_id,
                    KIND_TRANSCRIPT,
                    artifacts[KIND_TRANSCRIPT],
                    commit=False,
                )
                changed = True
            except (OSError, ValueError, TypeError) as exc:
                logging.warning(
                    "Could not import legacy transcript for %s: %s",
                    recording.recording_id,
                    exc,
                )

    transcripts = _parse_json(artifacts.get(KIND_TRANSCRIPT))
    if KIND_SENTENCES not in artifacts:
        document = None
        legacy_formatted = folder / "formatted.txt"
        if legacy_formatted.exists():
            try:
                formatted_text = legacy_formatted.read_text(encoding="utf-8")
                units = build_sentence_units(
                    transcripts if isinstance(transcripts, dict) else None,
                    formatted_text,
                )
                if units:
                    duration = float((transcripts or {}).get("duration") or 0.0)
                    document = {
                        "version": SCHEMA_VERSION,
                        "duration": round(duration, 3),
                        "sentences": [
                            {
                                "text": unit["text"],
                                "start": float(unit["start"]),
                                "end": float(unit["end"]),
                                "paragraph": int(unit["paragraph"]),
                                "segment_ids": [],
                            }
                            for unit in units
                        ],
                    }
            except (OSError, TypeError, ValueError) as exc:
                logging.warning(
                    "Could not import legacy formatted text for %s: %s",
                    recording.recording_id,
                    exc,
                )

        if document is None and isinstance(transcripts, dict):
            document = build_sentence_document(transcripts)

        if document is not None:
            artifacts[KIND_SENTENCES] = dump_json(document)
            await put_artifact(
                db,
                recording.recording_id,
                KIND_SENTENCES,
                artifacts[KIND_SENTENCES],
                commit=False,
            )
            changed = True

    if KIND_FORMATTED not in artifacts:
        sentences = _parse_json(artifacts.get(KIND_SENTENCES))
        if isinstance(sentences, dict):
            artifacts[KIND_FORMATTED] = render_formatted(
                sentences, max_line_length=MAX_LINE_LENGTH
            )
            await put_artifact(
                db,
                recording.recording_id,
                KIND_FORMATTED,
                artifacts[KIND_FORMATTED],
                commit=False,
            )
            changed = True
        else:
            legacy_formatted = folder / "formatted.txt"
            if legacy_formatted.exists():
                try:
                    artifacts[KIND_FORMATTED] = legacy_formatted.read_text(encoding="utf-8")
                    await put_artifact(
                        db,
                        recording.recording_id,
                        KIND_FORMATTED,
                        artifacts[KIND_FORMATTED],
                        commit=False,
                    )
                    changed = True
                except OSError as exc:
                    logging.warning(
                        "Could not import legacy formatted text for %s: %s",
                        recording.recording_id,
                        exc,
                    )

    sentences_content = artifacts.get(KIND_SENTENCES)
    if sentences_content:
        source_sha = text_sha256(sentences_content)
        source_document = _parse_json(sentences_content)
        source_count = len((source_document or {}).get("sentences") or [])
        for lang in settings.translation_target_languages:
            kind = translation_kind(lang)
            if kind in artifacts:
                continue
            path = folder / f"formatted-{lang}.txt"
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                logging.warning(
                    "Could not import legacy translation %s for %s: %s",
                    lang,
                    recording.recording_id,
                    exc,
                )
                continue
            lines = [
                line
                for paragraph in split_formatted_paragraphs(text)
                for line in paragraph
            ]
            if len(lines) != source_count:
                continue
            content = dump_json(
                {
                    "version": SCHEMA_VERSION,
                    "source_sha256": source_sha,
                    "sentences": lines,
                }
            )
            artifacts[kind] = content
            await put_artifact(
                db,
                recording.recording_id,
                kind,
                content,
                commit=False,
            )
            changed = True

    if commit and changed:
        await db.commit()
    return artifacts
