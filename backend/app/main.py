import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.api.api import api_router
from app.core.config import PROJECT_ROOT, prepare_runtime_directories, settings
from app.core.deployment import (
    AUTH_CSRF_COOKIE,
    AUTH_SESSION_COOKIE,
    DeploymentBoundaryMiddleware,
)
from app.core.redaction import install_redaction_filter, redact_validation_errors
from app.db.database import (
    AsyncSessionLocal,
    create_schema,
    validate_runtime_database_target,
)
from app.db.maintenance import runtime_state_lease
from app.db.uow import (
    DatabaseBusyError,
    commit as commit_uow,
    flush as flush_uow,
    rollback as rollback_uow,
)
from app.models import AgentRun, Owner, Plan, Session, UserProfile  # noqa: F401 - imports register every mapped entity
from app.outbox import drain_outbox, recover_interrupted_deliveries
from app.runtime.agent import AgentRuntime
from app.runtime.state import (
    NONTERMINAL_RUN_STATUSES,
    reconcile_run_after_restart,
    repair_child_terminal_projection,
)
from app.runtime.scheduler import proactive_scheduler
from app.runtime.tasks import start_tracked_task


install_redaction_filter(logging.getLogger())
deployment_policy = settings.deployment_policy


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
            await flush_uow(db)
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
        await commit_uow(db)


async def verify_database_writable() -> None:
    """Fail fast with a clear message when the local SQLite file is not writable."""
    async with AsyncSessionLocal() as db:
        try:
            # BEGIN IMMEDIATE proves that the main database can acquire a write
            # transaction without leaving a table, row, or schema revision.
            await db.execute(text("BEGIN IMMEDIATE"))
            await db.execute(text("SELECT 1"))
            await rollback_uow(db)
        except Exception as exc:
            await rollback_uow(db)
            raise RuntimeError(
                f"Database is not writable: {type(exc).__name__}"
            ) from exc


async def reconcile_interrupted_runs() -> list[str]:
    """Rebuild runnable work from durable state, never from the old task map."""
    async with AsyncSessionLocal() as db:
        interrupted = list((await db.execute(select(AgentRun).where(
            AgentRun.status.in_(NONTERMINAL_RUN_STATUSES)
        ))).scalars())
        terminal_children = list((await db.execute(select(AgentRun.id).where(
            AgentRun.parent_run_id.is_not(None),
            AgentRun.status.in_(["completed", "failed", "cancelled"]),
        ))).scalars())
        classifications: list[tuple[str, bool, str | None, str, str | None]] = []
        for run in interrupted:
            scope_is_valid = True
            if run.plan_id is not None:
                plan_status = (await db.execute(select(Plan.status).where(
                    Plan.id == run.plan_id,
                    Plan.owner_id == run.owner_id,
                ))).scalar_one_or_none()
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
                    parent and parent.status in NONTERMINAL_RUN_STATUSES
                )
            classifications.append((
                run.id,
                scope_is_valid,
                run.session_id,
                run.owner_id,
                run.parent_run_id,
            ))
        await commit_uow(db)
    resumable: list[str] = []
    for run_id, scope_valid, _session_id, _owner_id, _parent_id in classifications:
        if await reconcile_run_after_restart(
            AsyncSessionLocal,
            run_id,
            scope_valid=scope_valid,
        ):
            resumable.append(run_id)
    for child_id in terminal_children:
        await repair_child_terminal_projection(AsyncSessionLocal, child_id)
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
    # Hold the same cross-process lease used by backup/reset/restore for the
    # full service lifetime.  It is acquired before any directory creation or
    # database connection, so an idle server cannot race a state replacement.
    with runtime_state_lease(PROJECT_ROOT):
        validate_runtime_database_target(PROJECT_ROOT)
        prepare_runtime_directories()
        await create_schema(state_lease_held=True, state_root=PROJECT_ROOT)
        await verify_database_writable()
        await ensure_local_owner()
        # The exclusive runtime lease proves no live dispatcher owns an old
        # claim. Recover those crash states before the background worker may
        # claim new intents; uncertain SMTP/Web Push is never auto-replayed.
        await recover_interrupted_deliveries(session_factory=AsyncSessionLocal)
        resumable_runs = await reconcile_interrupted_runs()
        outbox_stop = asyncio.Event()
        outbox_task = asyncio.create_task(
            drain_outbox(outbox_stop, session_factory=AsyncSessionLocal)
        )
        proactive_scheduler.start()
        try:
            for run_id in resumable_runs:
                start_tracked_task(run_id, resume_interrupted_run(run_id))
            yield
        finally:
            await proactive_scheduler.stop()
            outbox_stop.set()
            outbox_task.cancel()
            with suppress(asyncio.CancelledError):
                await outbox_task


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def request_validation_response(_, exc: RequestValidationError) -> JSONResponse:
    """Never reflect configured credentials through FastAPI's 422 payload."""

    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(redact_validation_errors(exc.errors()))},
    )


@app.exception_handler(DatabaseBusyError)
async def database_busy_response(_, exc: DatabaseBusyError) -> JSONResponse:
    """Expose bounded SQLite contention as a typed retryable API state."""

    return JSONResponse(
        status_code=503,
        content=exc.as_result(),
        headers={"Retry-After": str(max(1, (exc.retry_after_ms + 999) // 1000))},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(deployment_policy.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)
app.add_middleware(DeploymentBoundaryMiddleware, policy=deployment_policy)


@app.post(f"{settings.API_V1_STR}/auth/session", include_in_schema=False)
async def create_authenticated_session(request: Request) -> JSONResponse:
    """Exchange the configured server bearer token for a short browser session."""

    if not deployment_policy.authentication_enabled:
        return JSONResponse({"detail": "not found"}, status_code=404)
    if not deployment_policy.authenticate_bearer(request.headers.get("authorization")):
        return JSONResponse(
            {"detail": "authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    session_token, csrf_token, expires_at = deployment_policy.issue_session()
    response = JSONResponse(
        {"authenticated": True, "csrf_token": csrf_token, "expires_at": expires_at}
    )
    response.set_cookie(
        AUTH_SESSION_COOKIE,
        session_token,
        max_age=deployment_policy.session_ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        AUTH_CSRF_COOKIE,
        csrf_token,
        max_age=deployment_policy.session_ttl_seconds,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )
    return response


@app.delete(f"{settings.API_V1_STR}/auth/session", include_in_schema=False)
async def delete_authenticated_session() -> JSONResponse:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        AUTH_SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        AUTH_CSRF_COOKIE,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )
    return response


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
