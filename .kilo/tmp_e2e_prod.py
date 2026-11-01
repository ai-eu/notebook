"""End-to-end check of the deployed translation through the running service."""

import asyncio
import sqlite3
import time

import httpx

BASE = "http://127.0.0.1:5003"
REC = "2026-10-15_09-22-16_hQqLDVQq"

con = sqlite3.connect("/opt/apps/notebook/app.db")
key = con.execute("select groq_key from users order by id limit 1").fetchone()[0]


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60.0) as client:
        login = await client.post("/api/auth/login", data={"key": key})
        print("login:", login.status_code, flush=True)
        started = time.monotonic()
        for _ in range(80):
            response = await client.get(f"/api/translate/{REC}", params={"lang": "uk"})
            if response.status_code == 202:
                body = response.json()
                print(
                    f"202 running {body.get('done')}/{body.get('total')} "
                    f"at {time.monotonic() - started:.0f}s",
                    flush=True,
                )
                await asyncio.sleep(5)
                continue
            print(
                f"final: {response.status_code} in {time.monotonic() - started:.1f}s "
                f"{response.text[:200]}",
                flush=True,
            )
            if response.status_code == 200:
                data = response.json()
                print(
                    f"sentences: {len(data['sentences'])}, cached: {data['cached']}, "
                    f"lang: {data['language']}",
                    flush=True,
                )
                print("sample:", data["sentences"][0][:90], flush=True)
            break
        await client.get("/api/auth/logout")


asyncio.run(main())
