"""Application settings (env-prefixed ``LF_``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LF_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/ledgerforge"
    debug: bool = False
    #: Extra directory scanned for drop-in plugins (colon-separated).
    plugin_path: str = ""
    #: Load the built-in example plugin (entry-point discovery) at startup.
    example_plugins: bool = True
    #: Allow voucher dates in the future (beyond tomorrow).
    allow_future_dates: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
