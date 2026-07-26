"""Alembic environment.

Migrations run against ``DATABASE_ADMIN_URL`` (the schema owner), never the
runtime ``app_rw`` role -- ``app_rw`` deliberately has no DDL rights.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing the models package registers every table on Base.metadata.
from api.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

dsn = os.environ.get("DATABASE_ADMIN_URL")
if not dsn:
    raise RuntimeError(
        "DATABASE_ADMIN_URL is not set. Migrations run as the schema owner; see .env.example."
    )
config.set_main_option("sqlalchemy.url", dsn)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # Autogenerate must not try to "helpfully" drop the RLS policies,
            # functions, roles and triggers created in 0002, none of which are
            # part of the SQLAlchemy metadata.
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


def _include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    if type_ == "table" and name == "alembic_version":
        return False
    return True


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
