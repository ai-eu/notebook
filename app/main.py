import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.config import BASE_DIR
from app.database import init_db
from app.routers import auth, pages, upload, recordings, tts, translations
from app.services.http import close_client
from app.services.jobs import job_runner
from app.services.processing import recover_interrupted_jobs
from app.services.storage.maintenance import maintenance_loop


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    await recover_interrupted_jobs()
    maintenance = asyncio.create_task(maintenance_loop())
    try:
        yield
    finally:
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        await job_runner.shutdown()
        await close_client()


app = FastAPI(title="Dictaphone Transcriber", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(upload.router)
app.include_router(recordings.router)
app.include_router(tts.router)
app.include_router(translations.router)
