import os
from pathlib import Path

import pytest_asyncio


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'test_learning_companion.db'}"
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["ENABLE_SCHEDULER"] = "false"

from app.db.database import AsyncSessionLocal, Base, engine  # noqa: E402
from app.models import Owner, UserProfile  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        db.add(Owner(id="local", display_name="Test learner", timezone="Asia/Shanghai"))
        db.add(UserProfile(owner_id="local"))
        await db.commit()
    yield
