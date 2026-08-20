from typing import Literal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.envfile import clear_env_keys, update_env_file
from app.core.config import settings
from app.core.redaction import redact_text
from app.db.database import get_db
from app.db.uow import DatabaseBusyError, commit as commit_uow
from app.models import Memory, Notification, Plan, Session, UserProfile
from app.notifications.diagnostics import (
    SMTP_DIAGNOSTIC_BODY,
    SMTP_DIAGNOSTIC_TITLE,
    email_configuration,
    require_smtp_configuration,
    test_imap,
    test_smtp,
)
from app.outbox import OutboxConflictError, enqueue_smtp_diagnostic
from app.tools.registry import tool_contracts
from sqlalchemy import func, select
from app.runtime.scheduler import proactive_scheduler


router = APIRouter()
NOTIFICATION_COOLDOWN_PREFERENCE = "notification_cooldown_minutes"


class EmailTestRequest(BaseModel):
    channel: Literal["smtp", "imap"]
    send_message: bool = False
    action_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_action_identity(self):
        if self.send_message and self.channel != "smtp":
            raise ValueError("send_message is supported only for SMTP diagnostics")
        if self.channel == "smtp" and self.send_message:
            normalized = (self.action_id or "").strip()
            if not normalized:
                raise ValueError("action_id is required when sending an SMTP diagnostic")
            self.action_id = normalized
        elif self.action_id is not None:
            raise ValueError("action_id is accepted only when sending an SMTP diagnostic")
        return self


class EmailSettingsUpdate(BaseModel):
    smtp_host: str | None = Field(default=None, max_length=300)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_username: str | None = Field(default=None, max_length=300)
    smtp_password: str | None = Field(default=None, max_length=300)
    smtp_from: str | None = Field(default=None, max_length=300)
    smtp_to: str | None = Field(default=None, max_length=300)
    smtp_use_tls: bool | None = None
    smtp_use_ssl: bool | None = None
    enable_email_reply_polling: bool | None = None
    imap_host: str | None = Field(default=None, max_length=300)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_username: str | None = Field(default=None, max_length=300)
    imap_password: str | None = Field(default=None, max_length=300)
    imap_folder: str | None = Field(default=None, max_length=200)


class ModelSettingsUpdate(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    temperature: float | None = Field(default=None, ge=0, le=2)


class NotificationPolicyUpdate(BaseModel):
    quiet_hours: dict | None = None
    daily_notification_limit: int | None = Field(default=None, ge=0, le=20)
    cooldown_minutes: int | None = Field(default=None, ge=0, le=1440)


class FollowUpBehaviorUpdate(BaseModel):
    follow_up_behavior: Literal["steer", "queue"]


class ProactivePauseUpdate(BaseModel):
    paused: bool


def _database_path_label() -> str:
    url = settings.DATABASE_URL
    if url.startswith("sqlite+aiosqlite:///"):
        raw = url[len("sqlite+aiosqlite:///"):]
        path = Path(raw)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        return str(path.resolve())
    return url


def _notification_cooldown(profile: UserProfile | None) -> int:
    if profile is None:
        return settings.AGENT_NOTIFICATION_COOLDOWN_MINUTES
    value = (profile.preferences or {}).get(NOTIFICATION_COOLDOWN_PREFERENCE)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1440:
        return settings.AGENT_NOTIFICATION_COOLDOWN_MINUTES
    return value


@router.get("")
async def read_settings(db: AsyncSession = Depends(get_db)):
    profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
    counts = {
        "plans": int((await db.scalar(
            select(func.count(Plan.id)).where(Plan.owner_id == settings.DEFAULT_OWNER_ID)
        )) or 0),
        "sessions": int((await db.scalar(
            select(func.count(Session.id)).where(Session.owner_id == settings.DEFAULT_OWNER_ID)
        )) or 0),
        "notifications": int((await db.scalar(
            select(func.count(Notification.id)).where(
                Notification.owner_id == settings.DEFAULT_OWNER_ID,
                Notification.channel == "in_app",
                Notification.archived_at.is_(None),
            )
        )) or 0),
        "memories": int((await db.scalar(
            select(func.count(Memory.id)).where(
                Memory.owner_id == settings.DEFAULT_OWNER_ID,
                Memory.status == "confirmed",
            )
        )) or 0),
    }
    return {
        "model": settings.MODEL_NAME,
        "base_url": settings.OPENAI_API_BASE,
        "api_key_configured": bool(settings.OPENAI_API_KEY),
        "model_context_window": settings.MODEL_CONTEXT_WINDOW,
        "context_token_budget": settings.AGENT_CONTEXT_TOKEN_BUDGET,
        "database_file": _database_path_label(),
        "data_counts": counts,
        "scheduler_enabled": settings.ENABLE_SCHEDULER,
        "heartbeat_seconds": settings.AGENT_HEARTBEAT_SECONDS,
        "email_configured": bool(settings.SMTP_HOST and settings.SMTP_USERNAME and settings.SMTP_PASSWORD and settings.SMTP_TO),
        "email_reply_configured": bool(
            settings.ENABLE_EMAIL_REPLY_POLLING
            and settings.IMAP_HOST
            and settings.IMAP_USERNAME
            and settings.IMAP_PASSWORD
        ),
        "push_configured": bool(
            settings.VAPID_PUBLIC_KEY
            and settings.VAPID_PRIVATE_KEY
            and settings.VAPID_SUBJECT
        ),
        "vapid_public_key": settings.VAPID_PUBLIC_KEY,
        "notification_cooldown_minutes": _notification_cooldown(profile),
        "timezone": settings.DEFAULT_TIMEZONE,
    }


@router.get("/tools")
async def read_tool_contracts():
    return {"count": len(tool_contracts()), "tools": tool_contracts()}


@router.get("/proactive")
async def read_proactive_status():
    return await proactive_scheduler.describe()


@router.get("/followup")
async def read_followup_behavior(db: AsyncSession = Depends(get_db)):
    profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
    return {"follow_up_behavior": profile.follow_up_behavior if profile else "steer"}


@router.put("/followup")
async def update_followup_behavior(
    data: FollowUpBehaviorUpdate,
    db: AsyncSession = Depends(get_db),
):
    profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
    if profile is None:
        profile = UserProfile(owner_id=settings.DEFAULT_OWNER_ID)
        db.add(profile)
    profile.follow_up_behavior = data.follow_up_behavior
    await commit_uow(db)
    return {"follow_up_behavior": profile.follow_up_behavior}


@router.put("/proactive")
async def update_proactive_pause(data: ProactivePauseUpdate, db: AsyncSession = Depends(get_db)):
    profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
    if profile is None:
        profile = UserProfile(owner_id=settings.DEFAULT_OWNER_ID)
        db.add(profile)
    profile.proactive_paused = data.paused
    await commit_uow(db)
    return {**await proactive_scheduler.describe(), "paused": profile.proactive_paused}


@router.get("/email")
async def read_email_configuration():
    return email_configuration()


@router.post("/email/test")
async def test_email_configuration(
    data: EmailTestRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        if data.channel == "smtp":
            if not data.send_message:
                return await test_smtp()
            configuration = require_smtp_configuration()
            action = await enqueue_smtp_diagnostic(
                db,
                owner_id=settings.DEFAULT_OWNER_ID,
                action_id=data.action_id or "",
                title=SMTP_DIAGNOSTIC_TITLE,
                body=SMTP_DIAGNOSTIC_BODY,
            )
            action_status = action.status
            action_key = action.action_key
            await commit_uow(db)
            return {
                "ok": True,
                "channel": "smtp",
                "status": action_status,
                "action_id": data.action_id,
                "action_key": action_key,
                "message_queued": action_status == "queued",
                "recipient": configuration["smtp_to"],
            }
        return await test_imap()
    except OutboxConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="SMTP diagnostic action_id conflicts with a different request",
        ) from exc
    except DatabaseBusyError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=redact_text(exc)) from exc
    except Exception as exc:
        detail = f"{data.channel.upper()} test failed: {type(exc).__name__}: {exc}"
        raise HTTPException(status_code=502, detail=redact_text(detail)) from exc


@router.put("/email", response_model=dict)
async def update_email_settings(data: EmailSettingsUpdate):
    values: dict[str, str] = {}
    string_fields = {
        "smtp_host": "SMTP_HOST",
        "smtp_username": "SMTP_USERNAME",
        "smtp_password": "SMTP_PASSWORD",
        "smtp_from": "SMTP_FROM",
        "smtp_to": "SMTP_TO",
        "imap_host": "IMAP_HOST",
        "imap_username": "IMAP_USERNAME",
        "imap_password": "IMAP_PASSWORD",
        "imap_folder": "IMAP_FOLDER",
    }
    bool_fields = {
        "smtp_use_tls": "SMTP_USE_TLS",
        "smtp_use_ssl": "SMTP_USE_SSL",
        "enable_email_reply_polling": "ENABLE_EMAIL_REPLY_POLLING",
    }
    int_fields = {"smtp_port": "SMTP_PORT", "imap_port": "IMAP_PORT"}
    for field, env_key in string_fields.items():
        value = getattr(data, field)
        if value is not None:
            values[env_key] = value
    for field, env_key in bool_fields.items():
        value = getattr(data, field)
        if value is not None:
            values[env_key] = "true" if value else "false"
    for field, env_key in int_fields.items():
        value = getattr(data, field)
        if value is not None:
            values[env_key] = str(value)
    if values:
        update_env_file(values)
    return {**email_configuration(), "restart_required": True}


@router.delete("/email", response_model=dict)
async def delete_email_credentials():
    clear_env_keys([
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "SMTP_TO",
        "SMTP_USE_TLS",
        "SMTP_USE_SSL",
        "ENABLE_EMAIL_REPLY_POLLING",
        "IMAP_HOST",
        "IMAP_PORT",
        "IMAP_USERNAME",
        "IMAP_PASSWORD",
        "IMAP_FOLDER",
    ])
    return {**email_configuration(), "restart_required": True}


@router.put("/model", response_model=dict)
async def update_model_settings(data: ModelSettingsUpdate):
    values: dict[str, str] = {}
    if data.base_url is not None:
        values["OPENAI_API_BASE"] = data.base_url
    if data.model is not None:
        values["MODEL_NAME"] = data.model
    if data.api_key:
        values["OPENAI_API_KEY"] = data.api_key
    if data.temperature is not None:
        values["MODEL_TEMPERATURE"] = str(data.temperature)
    if values:
        update_env_file(values)
    return {
        "restart_required": True,
        "model": data.model or settings.MODEL_NAME,
        "base_url": data.base_url or settings.OPENAI_API_BASE,
        "api_key_configured": bool(settings.OPENAI_API_KEY or data.api_key),
        "temperature": data.temperature if data.temperature is not None else settings.MODEL_TEMPERATURE,
    }


@router.put("/notification", response_model=dict)
async def update_notification_policy(
    data: NotificationPolicyUpdate,
    db: AsyncSession = Depends(get_db),
):
    profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    if data.quiet_hours is not None:
        profile.quiet_hours = data.quiet_hours
    if data.daily_notification_limit is not None:
        profile.daily_notification_limit = data.daily_notification_limit
    if data.cooldown_minutes is not None:
        preferences = dict(profile.preferences or {})
        preferences[NOTIFICATION_COOLDOWN_PREFERENCE] = data.cooldown_minutes
        profile.preferences = preferences
    await commit_uow(db)
    return {
        "restart_required": False,
        "quiet_hours": profile.quiet_hours,
        "daily_notification_limit": profile.daily_notification_limit,
        "cooldown_minutes": _notification_cooldown(profile),
    }
