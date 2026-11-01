import asyncio
import time

import httpx

from app.config import settings

KEY = settings.gemini_api_key
BASE = settings.gemini_base_url.rstrip("/")
NATIVE = "https://generativelanguage.googleapis.com/v1beta"


async def main() -> None:
    print("key prefix:", (KEY or "")[:8], "len:", len(KEY or ""), flush=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
        started = time.monotonic()
        try:
            listing = await client.get(f"{BASE}/models", headers={"Authorization": f"Bearer {KEY}"})
            print(f"models: {listing.status_code} in {time.monotonic() - started:.1f}s", flush=True)
        except httpx.HTTPError as exc:
            print(f"models: FAILED {type(exc).__name__}: {exc}", flush=True)

        for model in ("gemini-3.8-flash", "gemini-3.5-flash-lite"):
            started = time.monotonic()
            try:
                response = await client.post(
                    f"{BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {KEY}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Say OK"}],
                        "max_tokens": 40,
                    },
                )
                elapsed = time.monotonic() - started
                print(
                    f"openai-compat {model}: {response.status_code} in {elapsed:.1f}s "
                    f"{response.text[:120]}",
                    flush=True,
                )
            except httpx.HTTPError as exc:
                elapsed = time.monotonic() - started
                print(
                    f"openai-compat {model}: FAILED {type(exc).__name__} after {elapsed:.1f}s",
                    flush=True,
                )

        started = time.monotonic()
        try:
            response = await client.post(
                f"{NATIVE}/models/gemini-3.8-flash:generateContent",
                headers={"x-goog-api-key": f"{KEY}", "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": "Say OK"}]}]},
            )
            elapsed = time.monotonic() - started
            print(f"native 3.8-flash: {response.status_code} in {elapsed:.1f}s {response.text[:140]}", flush=True)
        except httpx.HTTPError as exc:
            elapsed = time.monotonic() - started
            print(f"native 3.8-flash: FAILED {type(exc).__name__} after {elapsed:.1f}s", flush=True)


asyncio.run(main())
