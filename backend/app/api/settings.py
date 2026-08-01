from fastapi import APIRouter

from app.core.config import settings
from app.tools.registry import tool_contracts


router = APIRouter()


@router.get("")
async def read_settings():
    return {
        "model": settings.MODEL_NAME,
        "base_url": settings.OPENAI_API_BASE,
        "api_key_configured": bool(settings.OPENAI_API_KEY),
        "scheduler_enabled": settings.ENABLE_SCHEDULER,
        "heartbeat_seconds": settings.AGENT_HEARTBEAT_SECONDS,
        "email_configured": bool(settings.SMTP_HOST and settings.SMTP_USERNAME and settings.SMTP_TO),
        "email_reply_configured": bool(
            settings.ENABLE_EMAIL_REPLY_POLLING
            and settings.IMAP_HOST
            and settings.IMAP_USERNAME
            and settings.IMAP_PASSWORD
        ),
        "timezone": settings.DEFAULT_TIMEZONE,
    }


@router.get("/tools")
async def read_tool_contracts():
    return {"count": len(tool_contracts()), "tools": tool_contracts()}
