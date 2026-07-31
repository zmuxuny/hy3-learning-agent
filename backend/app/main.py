from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.api.api import api_router
from app.core.config import PROJECT_ROOT, settings
from app.db.database import AsyncSessionLocal, create_schema
from app.models import Owner, UserProfile  # noqa: F401 - imports register every mapped entity
from app.runtime.scheduler import proactive_scheduler


async def ensure_local_owner() -> None:
    async with AsyncSessionLocal() as db:
        owner = await db.get(Owner, settings.DEFAULT_OWNER_ID)
        if owner is None:
            owner = Owner(
                id=settings.DEFAULT_OWNER_ID,
                display_name="Learner",
                timezone=settings.DEFAULT_TIMEZONE,
            )
            db.add(owner)
            await db.flush()
        profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
        if profile is None:
            db.add(
                UserProfile(
                    owner_id=settings.DEFAULT_OWNER_ID,
                    daily_notification_limit=settings.AGENT_DAILY_NOTIFICATION_LIMIT,
                )
            )
        elif profile.agent_style == "supervising_coach":
            profile.agent_style = "adaptive_study_partner"
        await db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await create_schema()
    await ensure_local_owner()
    proactive_scheduler.start()
    try:
        yield
    finally:
        await proactive_scheduler.stop()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix=settings.API_V1_STR)


frontend_dist = PROJECT_ROOT / "frontend" / "dist"
assets_dir = frontend_dist / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend(full_path: str):
    requested = frontend_dist / full_path
    if full_path and requested.is_file() and requested.resolve().is_relative_to(frontend_dist.resolve()):
        return FileResponse(requested)
    index = frontend_dist / "index.html"
    if index.exists():
        return FileResponse(index)
    return {
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "frontend": "not built; run npm ci && npm run build in frontend/",
    }
