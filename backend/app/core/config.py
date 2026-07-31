from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    PROJECT_NAME: str = "AI Learning Companion"
    VERSION: str = "0.2.0"
    API_V1_STR: str = "/api/v1"
    DEFAULT_OWNER_ID: str = "local"
    DEFAULT_TIMEZONE: str = "Asia/Shanghai"

    DATABASE_URL: str = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'learning_companion.db'}"

    OPENAI_API_KEY: str = ""
    OPENAI_API_BASE: str = "https://tokenhub.tencentmaas.com/v1"
    MODEL_NAME: str = "hy3"
    MODEL_TEMPERATURE: float = 0.9
    MODEL_REASONING_EFFORT: str = "high"

    AGENT_MAX_STEPS: int = Field(default=8, ge=1, le=24)
    AGENT_HEARTBEAT_SECONDS: int = Field(default=300, ge=15)
    AGENT_CONTEXT_EVENT_LIMIT: int = Field(default=40, ge=5, le=500)
    AGENT_DAILY_NOTIFICATION_LIMIT: int = Field(default=3, ge=0, le=20)
    AGENT_NOTIFICATION_COOLDOWN_MINUTES: int = Field(default=180, ge=0)
    ENABLE_SCHEDULER: bool = True

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_TO: str = ""
    SMTP_USE_TLS: bool = True

    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "data" / "context" / "plans").mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "data" / "context" / "decisions").mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
