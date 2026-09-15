from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.database import init_db
from app.routers import auth, pages, upload, recordings

app = FastAPI(title="Dictaphone Transcriber")

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(upload.router)
app.include_router(recordings.router)


@app.on_event("startup")
async def startup():
    await init_db()
