"""Compare Gemini flash models on one real batch: speed, format, typos."""

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
MIXED = re.compile(r"[а-яіїєґА-ЯІЇЄҐ][a-zA-Z]|[a-zA-Z][а-яіїєґА-ЯІЇЄҐ]")


def parse(content: str) -> list[str]:
    text = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = data.get("translations")
    return [str(item) for item in data] if isinstance(data, list) else []


MODELS = [
    ("gemini-3.5-flash-lite", {"reasoning_effort": "low"}),
    ("gemini-3.5-flash", {"reasoning_effort": "low"}),
    ("gemini-3.6-flash", {"reasoning_effort": "low"}),
]


async def main() -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=15.0)) as client:
        for model, extra in MODELS:
            started = time.monotonic()
            try:
                response = await client.post(
                    URL,
                    headers=HEADERS,
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "You are a helpful translator."},
                            {"role": "user", "content": PROMPT},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 2400,
                        **extra,
                    },
                )
            except httpx.HTTPError as exc:
                print(f"{model}: FAILED {type(exc).__name__} in "
                      f"{time.monotonic() - started:.1f}s", flush=True)
                continue
            elapsed = time.monotonic() - started
            if response.status_code != 200:
                print(f"{model}: HTTP {response.status_code} {response.text[:120]}", flush=True)
                continue
            data = response.json()
            choice = data["choices"][0]
            lines = parse(choice["message"].get("content") or "")
            typos = sum(1 for line in lines if MIXED.search(line))
            usage = data.get("usage", {})
            print(
                f"{model}: {elapsed:.1f}s items={len(lines)}/{len(batch)} typos={typos} "
                f"completion={usage.get('completion_tokens')} finish={choice.get('finish_reason')}",
                flush=True,
            )
            if lines:
                print(f"  {lines[0][:100]}", flush=True)


asyncio.run(main())
