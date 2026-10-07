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
    database_url: str = "sqlite+aiosqlite:///./desk_ticket.db"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "change-me"
    api_key: str = ""
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
        if len(self.api_key) < 16:
            raise ValueError("API_KEY must be set to at least 16 characters in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
