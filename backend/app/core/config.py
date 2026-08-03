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

    PROJECT_NAME: str = "Learning Agent"
    VERSION: str = "0.7.1"
    API_V1_STR: str = "/api/v1"
    DEFAULT_OWNER_ID: str = "local"
    DEFAULT_TIMEZONE: str = "Asia/Shanghai"

    DATABASE_URL: str = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'learning_companion.db'}"

    OPENAI_API_KEY: str = ""
    OPENAI_API_BASE: str = "https://tokenhub.tencentmaas.com/v1"
    MODEL_NAME: str = "hy3"
    MODEL_CONTEXT_WINDOW: int = Field(default=128000, ge=4096, le=1000000)
    MODEL_TEMPERATURE: float = 0.9
    MODEL_REASONING_EFFORT: str = "high"

    AGENT_MAX_STEPS: int = Field(default=8, ge=1, le=24)
    AGENT_MAX_MODEL_CALLS: int = Field(default=12, ge=1, le=100)
    AGENT_MAX_TOOL_CALLS: int = Field(default=32, ge=1, le=200)
    AGENT_MAX_ELAPSED_SECONDS: int = Field(default=600, ge=30, le=86400)
    AGENT_MAX_ESTIMATED_COST_USD: float = Field(default=0.0, ge=0)
    MODEL_INPUT_PRICE_PER_1M: float = Field(default=0.0, ge=0)
    MODEL_OUTPUT_PRICE_PER_1M: float = Field(default=0.0, ge=0)
    AGENT_MODEL_TIMEOUT_SECONDS: int = Field(default=90, ge=10, le=300)
    AGENT_MODEL_RETRY_ATTEMPTS: int = Field(default=2, ge=1, le=3)
    AGENT_TOOL_TIMEOUT_SECONDS: int = Field(default=35, ge=5, le=120)
    AGENT_TOOL_FAILURE_LIMIT: int = Field(default=2, ge=1, le=5)
    AGENT_TOOL_MESSAGE_CHAR_LIMIT: int = Field(default=16000, ge=2000, le=100000)
    AGENT_SESSION_TITLE_TIMEOUT_SECONDS: int = Field(default=15, ge=3, le=60)
    AGENT_HEARTBEAT_SECONDS: int = Field(default=300, ge=15)
    AGENT_PROGRESS_CHECKIN_HOURS: int = Field(default=24, ge=1, le=720)
    AGENT_CONTEXT_EVENT_LIMIT: int = Field(default=40, ge=5, le=500)
    AGENT_CONTEXT_TOKEN_BUDGET: int = Field(default=12000, ge=2000, le=100000)
    AGENT_RECENT_MESSAGE_LIMIT: int = Field(default=16, ge=4, le=100)
    AGENT_SESSION_COMPRESSION_THRESHOLD: int = Field(default=24, ge=8, le=500)
    AGENT_DAILY_NOTIFICATION_LIMIT: int = Field(default=3, ge=0, le=20)
    AGENT_NOTIFICATION_COOLDOWN_MINUTES: int = Field(default=180, ge=0)
    ENABLE_SCHEDULER: bool = True

    MEMORY_RETRIEVAL_PROVIDER: str = "local_hash"

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_TO: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False

    IMAP_HOST: str = ""
    IMAP_PORT: int = 993
    IMAP_USERNAME: str = ""
    IMAP_PASSWORD: str = ""
    IMAP_FOLDER: str = "INBOX"
    ENABLE_EMAIL_REPLY_POLLING: bool = False

    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: str = ""
    VAPID_SUBJECT: str = "mailto:learner@example.com"

    WEB_SEARCH_TIMEOUT_SECONDS: int = Field(default=12, ge=3, le=60)
    WEB_SEARCH_PROVIDER: str = "duckduckgo"
    WEB_SEARCH_FALLBACK_PROVIDER: str = "bing"
    WEB_MAX_REDIRECTS: int = Field(default=5, ge=0, le=10)
    WEB_ALLOW_SYNTHETIC_DNS: bool = True
    TOOL_EXECUTION_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=60)
    TOOL_OUTPUT_LIMIT: int = Field(default=12000, ge=1000, le=100000)

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
    (PROJECT_ROOT / "data" / "workspace").mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
