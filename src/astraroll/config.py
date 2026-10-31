from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://astraroll:local-dev-only@localhost:5432/astraroll"
    redis_url: str = "redis://localhost:6379/0"
    order_stream: str = "astraroll:orders:v1"
    relay_batch_size: int = Field(default=100, ge=1, le=1000)
    relay_poll_seconds: float = Field(default=0.5, gt=0)
    analytics_backend: Literal["unavailable", "postgres"] = "postgres"
    analytics_coverage_provider: str | None = "astraroll.aggregation.coverage:read_coverage"
    analytics_api_keys_json: SecretStr | None = None
    coverage_max_age_seconds: int = Field(default=86400, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
