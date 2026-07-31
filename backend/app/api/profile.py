from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.models import UserProfile
from app.schemas import ProfileRead, ProfileUpdate


router = APIRouter()


@router.get("", response_model=ProfileRead)
async def read_profile(db: AsyncSession = Depends(get_db)):
    profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@router.patch("", response_model=ProfileRead)
async def update_profile(data: ProfileUpdate, db: AsyncSession = Depends(get_db)):
    profile = await db.get(UserProfile, settings.DEFAULT_OWNER_ID)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    await db.commit()
    await db.refresh(profile)
    return profile
