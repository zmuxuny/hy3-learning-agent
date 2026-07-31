from fastapi import APIRouter

from app.api import agent, memories, notifications, operations, plans, profile, settings, system


api_router = APIRouter()
api_router.include_router(system.router, tags=["system"])
api_router.include_router(agent.router, prefix="/agent", tags=["agent"])
api_router.include_router(plans.router, prefix="/plans", tags=["plans"])
api_router.include_router(profile.router, prefix="/profile", tags=["profile"])
api_router.include_router(memories.router, prefix="/memories", tags=["memories"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(operations.router, prefix="/operations", tags=["operations"])
api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
