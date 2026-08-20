from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.deployment import DeploymentPolicy
from app.version import APPLICATION_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    PROJECT_NAME: str = "Learning Agent"
    VERSION: str = APPLICATION_VERSION
    API_V1_STR: str = "/api/v1"
    DEFAULT_OWNER_ID: str = "local"
    DEFAULT_TIMEZONE: str = "Asia/Shanghai"

    DEPLOYMENT_MODE: Literal["local", "server"] = "local"
    SERVER_AUTH_TOKEN: SecretStr = SecretStr("")
    SERVER_PUBLIC_ORIGIN: str = ""
    SERVER_SESSION_TTL_SECONDS: int = Field(default=28_800, ge=300, le=86_400)

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
    AGENT_RUN_LEASE_SECONDS: int = Field(default=900, ge=30, le=86400)
    AGENT_RUN_RETRY_BACKOFF_SECONDS: float = Field(default=0.25, ge=0, le=300)
    AGENT_RUN_MAX_RETRIES: int = Field(default=3, ge=0, le=20)
    AGENT_SESSION_TITLE_TIMEOUT_SECONDS: int = Field(default=15, ge=3, le=60)
    AGENT_HEARTBEAT_SECONDS: int = Field(default=300, ge=15)
    AGENT_PROGRESS_CHECKIN_HOURS: int = Field(default=24, ge=1, le=720)
    AGENT_CANDIDATE_COOLDOWN_MINUTES: int = Field(default=180, ge=0, le=10080)
    AGENT_CONTEXT_EVENT_LIMIT: int = Field(default=40, ge=5, le=500)
    AGENT_CONTEXT_TOKEN_BUDGET: int = Field(default=12000, ge=2000, le=100000)
    AGENT_OUTPUT_TOKEN_RESERVE: int = Field(default=4096, ge=256, le=100000)
    AGENT_TOOL_RESULT_TOKEN_RESERVE: int = Field(default=2048, ge=256, le=100000)
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

    WEB_SEARCH_PROVIDER: str = "duckduckgo"
    WEB_SEARCH_FALLBACK_PROVIDER: str = "bing"
    WEB_MAX_REDIRECTS: int = Field(default=5, ge=0, le=10)
    WEB_MAX_WIRE_BYTES: int = Field(default=2_000_000, ge=1_024, le=50_000_000)
    WEB_MAX_DECODED_BYTES: int = Field(default=4_000_000, ge=1_024, le=100_000_000)
    WEB_TOTAL_DEADLINE_SECONDS: int = Field(default=20, ge=1, le=120)
    TOOL_EXECUTION_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=60)
    TOOL_OUTPUT_LIMIT: int = Field(default=12000, ge=1000, le=100000)
    CODE_SANDBOX_PROVIDER: str = Field(
        default="none",
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )

    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    @model_validator(mode="after")
    def validate_runtime_boundary(self) -> "Settings":
        if self.WEB_MAX_DECODED_BYTES < self.WEB_MAX_WIRE_BYTES:
            raise ValueError("WEB_MAX_DECODED_BYTES must be at least WEB_MAX_WIRE_BYTES")
        DeploymentPolicy.from_settings(self)
        return self

    @property
    def deployment_policy(self) -> DeploymentPolicy:
        return DeploymentPolicy.from_settings(self)

    @property
    def cors_origins(self) -> list[str]:
        return list(self.deployment_policy.cors_origins)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def prepare_runtime_directories(root: Path | None = None) -> None:
    """Create runtime-owned directories at an explicit startup boundary."""

    runtime_root = root or PROJECT_ROOT
    (runtime_root / "data" / "context" / "plans").mkdir(parents=True, exist_ok=True)
    (runtime_root / "data" / "context" / "decisions").mkdir(parents=True, exist_ok=True)
    (runtime_root / "data" / "workspace").mkdir(parents=True, exist_ok=True)


settings = get_settings()
