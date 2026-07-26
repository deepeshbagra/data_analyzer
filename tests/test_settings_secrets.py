"""The guard that refuses to start with a published secret.

``.env.example`` is committed to a public repository. Until this guard existed
it also contained values that *worked*, which is the dangerous combination: a
credential anyone can read that is simultaneously the default a forgotten
environment variable falls back to. Every failure mode was silent -- the app
started, the tests passed, and the database was reachable with a password
published on GitHub.

So the guard is asserted the way the RLS suite is: by proving it rejects the
exact strings that are in this repository's git history.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from api.settings import (
    MIN_SECRET_LENGTH,
    PUBLISHED_SECRETS,
    Settings,
    WeakSecretError,
    check_secret_strength,
)


def _strong() -> str:
    return secrets.token_urlsafe(32)


def _config(**overrides: Any) -> dict[str, Any]:
    """A complete, valid, strong configuration. Overrides weaken one field."""
    pw = _strong()
    base: dict[str, Any] = {
        "environment": "production",
        "database_url": f"postgresql+psycopg://app_rw:{pw}@db:5432/analyzer",
        "database_admin_url": f"postgresql+psycopg://postgres:{_strong()}@db:5432/analyzer",
        "database_ro_url": f"postgresql+psycopg://app_ro:{_strong()}@db:5432/analyzer",
        "database_auth_url": f"postgresql+psycopg://app_auth:{_strong()}@db:5432/analyzer",
        "redis_url": "redis://redis:6379/0",
        "celery_broker_url": "redis://redis:6379/1",
        "celery_result_backend": "redis://redis:6379/2",
        "s3_access_key_id": _strong(),
        "s3_secret_access_key": _strong(),
        "jwt_secret": _strong(),
    }
    base.update(overrides)
    return base


def _check(**overrides: Any) -> Settings:
    """Build a Settings and run the guard, as ``get_settings`` does."""
    settings = Settings(**_config(**overrides))
    check_secret_strength(settings)
    return settings


def test_a_strong_production_configuration_is_accepted() -> None:
    """The guard must not be so strict that nothing valid gets through."""
    settings = _check()
    assert settings.environment == "production"


@pytest.mark.parametrize(
    "published",
    sorted(PUBLISHED_SECRETS - {"__GENERATE__"}),
)
def test_every_published_secret_is_rejected_as_a_jwt_secret(published: str) -> None:
    """Parametrised over the blocklist itself, so adding a value adds a test."""
    with pytest.raises(WeakSecretError, match="published or well-known"):
        _check(jwt_secret=published)


def test_the_exact_password_from_this_repos_git_history_is_rejected() -> None:
    """``app_rw_dev_password`` is readable by anyone who clones this project.

    It was the fallback in migration 0002 and the value in .env.example, so it
    is permanently in the history of a public repository. It must never again
    reach a running system.
    """
    with pytest.raises(WeakSecretError, match="published or well-known"):
        _check(database_url="postgresql+psycopg://app_rw:app_rw_dev_password@db:5432/analyzer")


def test_the_unreplaced_placeholder_is_rejected() -> None:
    """Copying .env.example without editing it must not produce a running app."""
    with pytest.raises(WeakSecretError, match="published or well-known"):
        _check(jwt_secret="__GENERATE__")


def test_a_short_but_unlisted_secret_is_rejected() -> None:
    """A blocklist alone only catches what someone thought of.

    ``hunter2`` is not in the list and never will be; the length floor is what
    catches the ones nobody enumerated.
    """
    with pytest.raises(WeakSecretError, match=f"shorter than {MIN_SECRET_LENGTH}"):
        _check(jwt_secret="hunter2")


def test_the_length_floor_is_exact() -> None:
    """Pinned because an off-by-one here silently widens the gate by one byte.

    The first version of this file used a 24-character string as its example
    of a "short" secret and the test failed -- correctly, since the floor is
    ``< 24``. Better to assert the boundary than to guess at it.
    """
    at_the_floor = "x" * MIN_SECRET_LENGTH
    _check(jwt_secret=at_the_floor)  # accepted

    with pytest.raises(WeakSecretError, match=f"shorter than {MIN_SECRET_LENGTH}"):
        _check(jwt_secret="x" * (MIN_SECRET_LENGTH - 1))


def test_the_error_names_the_variable_but_never_the_value() -> None:
    """This message lands in logs, crash reporters and terminal scrollback."""
    weak = "too-short"
    with pytest.raises(WeakSecretError) as exc:
        _check(jwt_secret=weak)
    message = str(exc.value)
    assert "JWT_SECRET" in message
    assert weak not in message


def test_several_offenders_are_reported_at_once() -> None:
    """Fixing one and rediscovering the next is how a rollout takes an hour."""
    with pytest.raises(WeakSecretError) as exc:
        _check(jwt_secret="postgres", s3_secret_access_key="minioadmin")
    message = str(exc.value)
    assert "JWT_SECRET" in message
    assert "S3_SECRET_ACCESS_KEY" in message


@pytest.mark.parametrize("environment", ["local", "test"])
def test_development_environments_are_exempt(environment: str) -> None:
    """Local dev is bound to the developer's machine, and forcing 32-character
    passwords there buys nothing while making onboarding worse."""
    settings = _check(
        environment=environment,
        jwt_secret="change-me-in-every-environment",
        s3_secret_access_key="minioadmin",
    )
    assert settings.environment == environment


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_no_environment_outside_local_and_test_is_exempt(environment: str) -> None:
    """Staging holds real customer documents as often as production does."""
    with pytest.raises(WeakSecretError):
        _check(environment=environment, jwt_secret="minioadmin")
