"""Application settings (env-prefixed ``LF_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

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

    # --- authentication (issue #1) --------------------------------------
    #: ``disabled`` = dev only: anonymous admin, every endpoint open.
    #: ``oidc`` = production: Bearer JWTs verified against the issuer.
    auth_mode: Literal["disabled", "oidc"] = "disabled"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    #: Optional explicit JWKS URL; otherwise discovered from the issuer.
    oidc_jwks_uri: str = ""
    #: Comma-separated claim paths checked for roles (default covers
    #: ``roles`` and Keycloak's ``realm_access.roles``).
    oidc_roles_claims: str = "roles,realm_access.roles"
    #: Comma-separated extra CORS origins when auth is on (the UI is
    #: same-origin via the /api proxy; empty = no cross-origin calls).
    cors_origins: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def roles_claim_paths(self) -> tuple[str, ...]:
        paths = tuple(path.strip() for path in self.oidc_roles_claims.split(",") if path.strip())
        return paths or ("roles", "realm_access.roles")


@lru_cache
def get_settings() -> Settings:
    return Settings()
