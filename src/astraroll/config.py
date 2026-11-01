from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://astraroll:local-dev-only@localhost:5432/astraroll"
    deployment_mode: Literal["shared", "database_per_tenant"] = "shared"
    control_database_url: str | None = None
    tenant_database_prefix: str = "astraroll_t_"
    max_active_tenant_pools: int = Field(default=25, ge=1, le=200)
    tenant_pool_size: int = Field(default=2, ge=1, le=10)
    tenant_pool_max_overflow: int = Field(default=2, ge=0, le=10)
    redis_url: str = "redis://localhost:6379/0"
    order_stream: str = "astraroll:orders:v1"
    relay_batch_size: int = Field(default=100, ge=1, le=1000)
    relay_poll_seconds: float = Field(default=0.1, gt=0)
    analytics_read_block_reason: str | None = None
    analytics_backend: Literal["unavailable", "postgres"] = "postgres"
    analytics_coverage_provider: str | None = "astraroll.aggregation.coverage:read_coverage"
    analytics_api_keys_json: SecretStr | None = None
    coverage_max_age_seconds: int = Field(default=86400, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
