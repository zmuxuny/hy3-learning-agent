from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.envfile import clear_env_keys, update_env_file
from app.core.config import settings
from app.db.database import get_db
from app.models import UserProfile
from app.tools.registry import tool_contracts
from app.notifications.diagnostics import email_configuration, test_imap, test_smtp
from app.runtime.scheduler import proactive_scheduler


router = APIRouter()


class EmailTestRequest(BaseModel):
    channel: Literal["smtp", "imap"]
    send_message: bool = False


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


@router.get("")
async def read_settings():
    return {
        "model": settings.MODEL_NAME,
        "base_url": settings.OPENAI_API_BASE,
        "api_key_configured": bool(settings.OPENAI_API_KEY),
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
        "notification_cooldown_minutes": settings.AGENT_NOTIFICATION_COOLDOWN_MINUTES,
        "timezone": settings.DEFAULT_TIMEZONE,
    }


@router.get("/tools")
async def read_tool_contracts():
    return {"count": len(tool_contracts()), "tools": tool_contracts()}


@router.get("/proactive")
async def read_proactive_status():
    return await proactive_scheduler.describe()


@router.get("/email")
async def read_email_configuration():
    return email_configuration()


@router.post("/email/test")
async def test_email_configuration(data: EmailTestRequest):
    try:
        if data.channel == "smtp":
            return await test_smtp(send_message=data.send_message)
        return await test_imap()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{data.channel.upper()} test failed: {type(exc).__name__}: {exc}") from exc


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
    await db.commit()
    if data.cooldown_minutes is not None:
        update_env_file({"AGENT_NOTIFICATION_COOLDOWN_MINUTES": str(data.cooldown_minutes)})
    return {
        "restart_required": data.cooldown_minutes is not None,
        "quiet_hours": profile.quiet_hours,
        "daily_notification_limit": profile.daily_notification_limit,
        "cooldown_minutes": data.cooldown_minutes or settings.AGENT_NOTIFICATION_COOLDOWN_MINUTES,
    }
