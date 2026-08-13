from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.api.api import api_router
from app.core.config import PROJECT_ROOT, settings
from app.db.database import AsyncSessionLocal, create_schema
from app.models import AgentRun, Owner, Plan, Session, UserProfile  # noqa: F401 - imports register every mapped entity
from app.runtime.agent import AgentRuntime
from app.runtime.events import emit_event
from app.runtime.scheduler import proactive_scheduler
from app.runtime.tasks import start_tracked_task


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


async def verify_database_writable() -> None:
    """Fail fast with a clear message when the local SQLite file is not writable."""
    async with AsyncSessionLocal() as db:
        try:
            await db.execute(text("CREATE TABLE IF NOT EXISTS _write_probe (id INTEGER)"))
            await db.execute(text("DELETE FROM _write_probe"))
            await db.commit()
        except Exception as exc:
            raise RuntimeError(
                f"Database is not writable ({settings.DATABASE_URL}): {type(exc).__name__}: {exc}"
            ) from exc


async def reconcile_interrupted_runs() -> list[str]:
    """Recover checkpointed Runs and safely close in-memory Runs left by a previous process."""
    async with AsyncSessionLocal() as db:
        interrupted = list((await db.execute(
            select(AgentRun).where(AgentRun.status.in_(["queued", "running"]))
        )).scalars())
        if not interrupted:
            return []
        now = datetime.now(timezone.utc)
        resumable: list[str] = []
        failed_sessions: set[tuple[str, str]] = set()
        for run in interrupted:
            if run.checkpoint:
                scope_is_valid = True
                if run.plan_id is not None:
                    plan_status = (await db.execute(
                        select(Plan.status).where(
                            Plan.id == run.plan_id,
                            Plan.owner_id == run.owner_id,
                        )
                    )).scalar_one_or_none()
                    scope_is_valid = bool(plan_status and plan_status != "archived")
                if scope_is_valid and run.session_id is not None:
                    session = await db.get(Session, run.session_id)
                    scope_is_valid = bool(
                        session
                        and session.owner_id == run.owner_id
                        and session.archived_at is None
                        and session.plan_id == run.plan_id
                    )
                if scope_is_valid and run.parent_run_id is not None:
                    parent = await db.get(AgentRun, run.parent_run_id)
                    scope_is_valid = bool(
                        parent and parent.status in {"queued", "running", "waiting_approval"}
                    )
                if scope_is_valid:
                    run.status = "queued"
                    run.checkpoint = dict(run.checkpoint)
                    resumable.append(run.id)
                else:
                    run.status = "failed"
                    run.completed_at = now
            else:
                run.status = "failed"
                run.completed_at = now
            if (
                run.status == "failed"
                and run.parent_run_id is None
                and run.session_id is not None
            ):
                failed_sessions.add((run.owner_id, run.session_id))
        await db.commit()
        for run in interrupted:
            if run.id not in resumable:
                await emit_event(
                    db,
                    run.id,
                    "run.failed",
                    "上一次应用进程结束，本 Run 已安全收口；既有消息、工具结果和操作记录均保留。",
                    {"code": "process_interrupted", "recoverable": False},
                )
        if failed_sessions:
            from app.services.queue import dispatch_next_queued_message

            for owner_id, session_id in failed_sessions:
                next_run = await dispatch_next_queued_message(
                    db,
                    owner_id=owner_id,
                    session_id=session_id,
                )
                if next_run is not None:
                    start_tracked_task(next_run.id, AgentRuntime().run(next_run.id))
        return resumable


async def resume_interrupted_run(run_id: str) -> None:
    """Dispatch a recovered checkpoint to the runtime that created it."""
    async with AsyncSessionLocal() as db:
        run = await db.get(AgentRun, run_id)
        if run is None:
            return
        checkpoint_kind = (run.checkpoint or {}).get("kind")
        trigger = run.trigger
    if trigger == "subagent" and checkpoint_kind == "subagent":
        from app.tools.subagents import resume_subagent_run

        await resume_subagent_run(run_id)
        return
    await AgentRuntime().run(run_id, resume=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await create_schema()
    await verify_database_writable()
    await ensure_local_owner()
    resumable_runs = await reconcile_interrupted_runs()
    proactive_scheduler.start()
    try:
        for run_id in resumable_runs:
            start_tracked_task(run_id, resume_interrupted_run(run_id))
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
