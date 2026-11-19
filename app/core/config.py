import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_WEAK_SECRETS = {"change-me", "changeme", "replace-me", "secret", "password"}


class Settings(BaseSettings):
    app_name: str = "desk-ticket-api"
    environment: Literal["local", "staging", "production"] = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    database_url: str = "postgresql+asyncpg://desk:desk@localhost:5432/desk"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "change-me"
    access_token_ttl_minutes: int = Field(default=60, ge=1, le=1440)
    # Ingestion worker. See app/ingestion/worker.py.
    # Seconds to sleep when no job is due. Lower means faster pickup, more idle queries.
    ingestion_poll_seconds: float = Field(default=1.0, gt=0)
    # How long a claimed job is reserved. Must be longer than the slowest job, or a
    # second worker will start the same job while the first is still on it.
    ingestion_lease_seconds: int = Field(default=300, ge=10)
    ingestion_max_attempts: int = Field(default=5, ge=1)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    log_level: str = "INFO"
    log_json: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return json.loads(raw)
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def reject_unsafe_production_config(self) -> "Settings":
        if self.environment != "production":
            return self
        if self.debug:
            raise ValueError("DEBUG must be false in production")
        if len(self.secret_key) < 32 or self.secret_key.lower() in _WEAK_SECRETS:
            raise ValueError("SECRET_KEY must be a unique value of at least 32 characters")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
