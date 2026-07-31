from fastapi import APIRouter

from app.core.config import settings


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
        "timezone": settings.DEFAULT_TIMEZONE,
    }
