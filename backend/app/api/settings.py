from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.tools.registry import tool_contracts
from app.notifications.diagnostics import email_configuration, test_imap, test_smtp
from app.runtime.scheduler import proactive_scheduler


router = APIRouter()


class EmailTestRequest(BaseModel):
    channel: Literal["smtp", "imap"]
    send_message: bool = False


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
