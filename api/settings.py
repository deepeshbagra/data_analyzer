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


#: Environments where a weak or published secret is tolerated, because nothing
#: is reachable from outside the developer's machine.
DEVELOPMENT_ENVIRONMENTS = frozenset({"local", "test"})

#: Secrets that are published in this repository's git history, plus the usual
#: suspects. None of these may be used anywhere real.
PUBLISHED_SECRETS = frozenset(
    {
        "postgres",
        "app_rw_dev_password",
        "app_ro_dev_password",
        "app_auth_dev_password",
        "minioadmin",
        "change-me-in-every-environment",
        "changeme",
        "change-me",
        "password",
        "secret",
        "admin",
        "__GENERATE__",
    }
)

MIN_SECRET_LENGTH = 24


def _dsn_password(dsn: PostgresDsn | None) -> str | None:
    """The password out of a DSN.

    Pydantic v2 models ``PostgresDsn`` as a multi-host URL, so the credentials
    live in ``hosts()`` rather than on the object. Reaching for ``.password``
    raises ``AttributeError`` -- which, in a validator, would have surfaced as
    a confusing config error rather than as "your secret is weak".
    """
    if dsn is None:
        return None
    hosts = dsn.hosts()
    if not hosts:
        return None
    password = hosts[0].get("password")
    return password if password else None


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


class WeakSecretError(RuntimeError):
    """A secret is published, well-known, or too short for a real environment."""


def check_secret_strength(settings: Settings) -> None:
    """Refuse a configuration whose secrets are published or trivially weak.

    The same shape as the superuser check in ``GET /ready``: make the dangerous
    configuration impossible to run rather than documented as something not to
    do. It exists because ``.env.example`` is public and, until this guard,
    held values that actually worked -- a published credential that is also a
    functioning default is one forgotten environment variable away from a live
    database anyone can open. Every failure mode is silent: the app starts, the
    tests pass, nothing looks wrong.

    **Not a pydantic validator, deliberately.** It was one, and that leaked.
    Pydantic appends ``input_value={...}`` to every ``ValidationError``, and
    the input to a settings model is the raw environment -- so a guard whose
    entire purpose is protecting secrets printed them into the crash log of the
    one boot that failed it. Raising from plain Python keeps the message to
    exactly what is written below.

    Raises:
        WeakSecretError: naming the offending variables and never their values.
    """
    if settings.environment in DEVELOPMENT_ENVIRONMENTS:
        return

    candidates: list[tuple[str, str | None]] = [
        ("JWT_SECRET", settings.jwt_secret.get_secret_value()),
        ("S3_SECRET_ACCESS_KEY", settings.s3_secret_access_key.get_secret_value()),
        ("S3_ACCESS_KEY_ID", settings.s3_access_key_id.get_secret_value()),
        ("DATABASE_URL", _dsn_password(settings.database_url)),
        ("DATABASE_ADMIN_URL", _dsn_password(settings.database_admin_url)),
        ("DATABASE_RO_URL", _dsn_password(settings.database_ro_url)),
        ("DATABASE_AUTH_URL", _dsn_password(settings.database_auth_url)),
    ]

    published = sorted({n for n, v in candidates if v and v in PUBLISHED_SECRETS})
    short = sorted(
        {
            n
            for n, v in candidates
            if v and v not in PUBLISHED_SECRETS and len(v) < MIN_SECRET_LENGTH
        }
    )

    problems: list[str] = []
    if published:
        problems.append(f"published or well-known value: {', '.join(published)}")
    if short:
        problems.append(f"shorter than {MIN_SECRET_LENGTH} characters: {', '.join(short)}")
    if problems:
        raise WeakSecretError(
            f"refusing to start in environment {settings.environment!r} -- "
            + "; ".join(problems)
            + '. Generate replacements with `python -c "import secrets; '
            'print(secrets.token_urlsafe(32))"`.'
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Cached so ``.env`` is parsed once.

    Secret strength is checked here rather than on the model so that a failure
    carries our message and nothing else. Anything constructing ``Settings``
    directly bypasses it -- which is why the project rule is that config comes
    from this function.
    """
    # No arguments: every field is populated from the environment or .env by
    # pydantic-settings. This used to carry a `type: ignore[call-arg]`; the
    # pydantic mypy plugin now models BaseSettings correctly, so strict mode
    # flags that ignore as unused.
    settings = Settings()
    check_secret_strength(settings)
    return settings
