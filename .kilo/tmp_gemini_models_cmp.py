"""Compare Gemini models on the real transcript through the shipped app code."""

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/apps/notebook")

from app.config import settings  # noqa: E402
from app.services.formatter import translate_sentences  # noqa: E402

REC = "2026-10-15_09-22-16_hQqLDVQq"
con = sqlite3.connect("/opt/apps/notebook/app.db")
row = con.execute(
    "select content from recording_artifacts where recording_id=? and kind='sentences'",
    (REC,),
).fetchone()
sentences = [s["text"] for s in json.loads(row[0])["sentences"]]


async def main() -> None:
    print(f"sentences: {len(sentences)}, chars: {sum(len(s) for s in sentences)}", flush=True)
    for model in ("gemini-3.5-flash-lite", "gemini-3.8-flash"):
        settings.gemini_model = model
        started = time.monotonic()
        try:
            translated = await translate_sentences(sentences, "uk", None)
        except Exception as exc:  # noqa: BLE001 - probe wants the reason
            print(f"{model}: FAILED after {time.monotonic() - started:.1f}s: {exc}", flush=True)
            continue
        elapsed = time.monotonic() - started
        empty = sum(1 for line in translated if not line.strip())
        print(
            f"{model}: {len(translated)}/{len(sentences)} lines, empty={empty}, "
            f"{elapsed:.1f}s",
            flush=True,
        )
        print(f"  sample: {translated[0][:110]}", flush=True)
        print(f"  sample: {translated[5][:110]}", flush=True)


asyncio.run(main())
