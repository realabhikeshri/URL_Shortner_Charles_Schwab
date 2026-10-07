# lineage: REQ-GF-001 / implement.foundation, REQ-BF-002 / implement.analytics,
#          REQ-AMBIGUOUS / implement.enterprise_wiring, implement.api_key_auth
"""Runtime configuration, loaded from the environment (prefix ``SHORTENER_``)."""

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHORTENER_", env_file=".env", extra="ignore")

    app_name: str = "url-shortener"
    environment: Literal["dev", "test", "prod"] = "dev"
    base_url: str = "http://localhost:8000"
    database_url: str = "sqlite+aiosqlite:///./shortener.db"
    db_auto_migrate: bool = True
    db_pool_size: int = Field(10, ge=1, le=100)

    redirect_status_code: Literal[302, 307] = 302
    code_length: int = Field(7, ge=5, le=12)
    code_max_attempts: int = Field(5, ge=1, le=20)
    allow_private_hosts: bool = False
    max_url_length: int = Field(2048, ge=64, le=8192)
    max_request_body_bytes: int = Field(16_384, ge=1024)

    cors_origins: list[str] = Field(default_factory=list)
    hsts_enabled: bool = False
    log_level: str = "INFO"
    log_json: bool = True

    analytics_enabled: bool = True
    # Set SHORTENER_ANALYTICS_SALT_SECRET in production; the random default means unique-visitor
    # hashes are only stable for the lifetime of one process.
    analytics_salt_secret: SecretStr = Field(
        default_factory=lambda: SecretStr(secrets.token_hex(32))
    )
    analytics_retention_days: int = Field(90, ge=1, le=3650)
    analytics_queue_size: int = Field(10_000, ge=1)
    analytics_batch_size: int = Field(200, ge=1, le=10_000)
    analytics_purge_interval_s: float = Field(3600.0, gt=0)

    # Shared Redis for cache + rate limits (multi-replica); in-process fallbacks when unset.
    redis_url: str | None = None
    cache_enabled: bool = True
    cache_ttl_s: float = Field(300.0, gt=0)
    cache_negative_ttl_s: float = Field(5.0, gt=0)
    cache_max_entries: int = Field(10_000, ge=1)
    rate_limit_enabled: bool = True
    rate_limit_write_capacity: int = Field(60, ge=1)
    rate_limit_write_refill_per_s: float = Field(1.0, gt=0)
    rate_limit_redirect_capacity: int = Field(300, ge=1)
    rate_limit_redirect_refill_per_s: float = Field(50.0, gt=0)
    metrics_enabled: bool = True
    # Write endpoints require X-API-Key (redirects, metadata and stats stay public).
    auth_enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("redirect_status_code", mode="before")
    @classmethod
    def _coerce_status(cls, value: object) -> object:
        # Environment variables arrive as strings; Literal[302, 307] needs an int.
        return int(value) if isinstance(value, str) and value.strip().isdigit() else value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
