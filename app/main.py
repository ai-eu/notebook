import asyncio

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.database import init_db
from app.routers import auth, pages, upload, recordings
from app.services.storage.maintenance import maintenance_loop

app = FastAPI(title="Dictaphone Transcriber")

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(upload.router)
app.include_router(recordings.router)

# Keeps a reference to the background task so it is not garbage-collected.
_maintenance_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup():
    global _maintenance_task
    await init_db()
    _maintenance_task = asyncio.create_task(maintenance_loop())
