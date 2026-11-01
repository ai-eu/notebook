"""Pick sensible Gemini settings: format of the answer and the thinking budget."""

import asyncio
import json
import re
import sqlite3
import time

import httpx

from app.config import settings

REC = "2026-10-15_09-22-16_hQqLDVQq"
con = sqlite3.connect("/opt/apps/notebook/app.db")
row = con.execute(
    "select content from recording_artifacts where recording_id=? and kind='sentences'",
    (REC,),
).fetchone()
sentences = [s["text"] for s in json.loads(row[0])["sentences"]]
batch = sentences[:21]
batch_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(batch))

PROMPT = (
    "You are a professional translator. Translate the following numbered sentences into "
    "Ukrainian (uk).\nRules:\n1. Return exactly one translation per input line.\n"
    "2. Keep the same line order.\n3. Do not add, remove, or merge sentences.\n"
    "4. Do not include the numbering in the output.\n5. Do not add any commentary.\n"
    "6. Preserve names, numbers, and proper nouns as-is.\n7. Translate naturally into "
    "Ukrainian.\n\nReturn a JSON object with one key, \"translations\", containing exactly "
    f"{len(batch)} strings in the same order.\n\nInput:\n{batch_text}\n\nOutput:\n"
)

URL = f"{settings.gemini_base_url.rstrip('/')}/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {settings.gemini_api_key}",
    "Content-Type": "application/json",
}


def parse(content: str) -> int:
    text = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        return -1
    if isinstance(data, dict):
        data = data.get("translations")
    return len(data) if isinstance(data, list) else -2


CASES = [
    ("gemini-3.5-flash-lite", 2400, {}),
    ("gemini-3.8-flash", 2400, {"reasoning_effort": "low"}),
    ("gemini-3.8-flash", 6000, {"reasoning_effort": "none"}),
]


async def main() -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
        for model, max_tokens, extra in CASES:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a helpful translator."},
                    {"role": "user", "content": PROMPT},
                ],
                "temperature": 0.3,
                "max_tokens": max_tokens,
                **extra,
            }
            started = time.monotonic()
            try:
                response = await client.post(URL, headers=HEADERS, json=payload)
            except httpx.HTTPError as exc:
                print(f"{model} {extra}: FAILED {type(exc).__name__}", flush=True)
                continue
            elapsed = time.monotonic() - started
            if response.status_code != 200:
                print(
                    f"{model} {extra}: HTTP {response.status_code} {response.text[:150]}",
                    flush=True,
                )
                continue
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            usage = data.get("usage", {})
            items = parse(content)
            print(
                f"{model} max_tokens={max_tokens} {extra}: {elapsed:.1f}s "
                f"finish={choice.get('finish_reason')} items={items}/{len(batch)} "
                f"prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')} "
                f"chars={len(content)}",
                flush=True,
            )
            print(f"  head: {content[:90]!r}", flush=True)


asyncio.run(main())
