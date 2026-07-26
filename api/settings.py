"""Central application configuration.

Every environment-dependent value enters the process here. Modules import
``settings``; they never read ``os.environ`` directly.
"""

from __future__ import annotations

import decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Money is Decimal end to end. 28 digits of precision comfortably covers
# numeric(18,4) arithmetic, and ROUND_HALF_UP matches how Indian tax
# computation is conventionally rounded (ROUND_HALF_EVEN, Python's default,
# would disagree with a customer's own invoice on x.xx5 cases).
decimal.DefaultContext.prec = 28
decimal.DefaultContext.rounding = decimal.ROUND_HALF_UP
decimal.setcontext(decimal.DefaultContext)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: str = "INFO"
    api_base_url: str = "http://localhost:8000"

    # --- Databases ---------------------------------------------------------
    # Four DSNs, four privilege levels. See docs/DECISIONS.md.
    #   database_url       app_rw   runtime; RLS-enforced, non-superuser
    #   database_admin_url postgres schema owner; migrations and test setup
    #   database_ro_url    app_ro   SELECT-only; Phase 5 text-to-SQL
    #   database_auth_url  app_auth BYPASSRLS on app_user only; login lookup
    database_url: PostgresDsn
    database_admin_url: PostgresDsn
    database_ro_url: PostgresDsn | None = None
    database_auth_url: PostgresDsn | None = None

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- Redis / Celery ----------------------------------------------------
    redis_url: RedisDsn
    celery_broker_url: RedisDsn
    celery_result_backend: RedisDsn

    # --- Object storage ----------------------------------------------------
    s3_endpoint_url: str | None = None
    s3_region: str = "ap-south-1"
    s3_bucket: str = "documents"
    s3_access_key_id: SecretStr
    s3_secret_access_key: SecretStr
    s3_force_path_style: bool = True
    #: Presigned document URLs are short-lived: one lands in a browser history
    #: and a server log the moment the review UI renders a page.
    s3_presign_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    # --- Uploads -----------------------------------------------------------
    #: Enforced while streaming, so an oversized upload is abandoned rather
    #: than buffered. Indian bank statements for a full year run to a few MB;
    #: 50 MiB leaves room for a scanned annexure without inviting abuse.
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)

    # --- Auth --------------------------------------------------------------
    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_seconds: int = Field(default=900, ge=60)
    jwt_refresh_ttl_seconds: int = Field(default=2_592_000, ge=3600)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Cached so ``.env`` is parsed once."""
    # No arguments: every field is populated from the environment or .env by
    # pydantic-settings. This used to carry a `type: ignore[call-arg]`; the
    # pydantic mypy plugin now models BaseSettings correctly, so strict mode
    # flags that ignore as unused.
    return Settings()
