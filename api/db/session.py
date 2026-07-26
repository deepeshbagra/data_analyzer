"""Engines and session factories, one per privilege level.

The only way to touch tenant data at runtime is :func:`tenant_session`. It
opens a transaction and sets ``app.tenant_id`` *inside* that transaction, which
is what the row-level security policies read. Two properties follow:

* ``SET LOCAL`` semantics mean the setting dies with the transaction, so a
  pooled connection cannot carry one tenant's context into the next request.
* If the setting is absent, ``current_tenant_id()`` returns NULL, every policy
  comparison evaluates to NULL, and queries return zero rows. Forgetting to
  scope a session yields no data rather than all data -- it fails closed.

Never construct a ``Session`` against the read-write engine by hand.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from api.settings import get_settings

_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")
_SET_USER = text("SELECT set_config('app.user_id', :user_id, true)")


def _build_engine(dsn: str, *, pool: bool = True) -> Engine:
    settings = get_settings()
    return create_engine(
        dsn,
        echo=settings.db_echo,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size if pool else 1,
        max_overflow=settings.db_max_overflow if pool else 0,
        # Every unit of work is an explicit transaction; we manage BEGIN
        # ourselves so SET LOCAL is guaranteed to have a transaction to be
        # local to.
        future=True,
    )


@lru_cache(maxsize=1)
def rw_engine() -> Engine:
    """Runtime engine. Connects as ``app_rw``: non-superuser, RLS applies."""
    return _build_engine(str(get_settings().database_url))


@lru_cache(maxsize=1)
def admin_engine() -> Engine:
    """Schema owner. Migrations and test fixtures only -- never request paths."""
    return _build_engine(str(get_settings().database_admin_url), pool=False)


@lru_cache(maxsize=1)
def ro_engine() -> Engine:
    """SELECT-only engine for Phase 5 generated SQL. Grants, not trust."""
    settings = get_settings()
    if settings.database_ro_url is None:
        raise RuntimeError("DATABASE_RO_URL is not configured")
    return _build_engine(str(settings.database_ro_url))


_rw_sessions = sessionmaker(class_=Session, expire_on_commit=False)
_admin_sessions = sessionmaker(class_=Session, expire_on_commit=False)
_ro_sessions = sessionmaker(class_=Session, expire_on_commit=False)


@contextmanager
def tenant_session(
    tenant_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
) -> Iterator[Session]:
    """Open an RLS-scoped transaction for one tenant.

    Args:
        tenant_id: the only tenant whose rows this session can read or write.
        user_id: acting user, if any. Read by the ``membership`` policy so a
            user can enumerate their own tenants, and used to populate
            ``audit_log.actor``.

    Commits on clean exit, rolls back on exception.
    """
    session = _rw_sessions(bind=rw_engine())
    try:
        session.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        session.execute(_SET_USER, {"user_id": str(user_id) if user_id else None})
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def admin_session() -> Iterator[Session]:
    """Superuser session. Bypasses RLS -- migrations and test setup only.

    Anything in a request path that reaches for this is a bug.
    """
    session = _admin_sessions(bind=admin_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def readonly_session(tenant_id: uuid.UUID) -> Iterator[Session]:
    """Scoped SELECT-only session used to execute model-generated SQL.

    Still tenant-scoped: the ``app_ro`` role is subject to the same policies,
    so the SELECT-only grant and RLS are two independent barriers.
    """
    session = _ro_sessions(bind=ro_engine())
    try:
        session.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        yield session
    finally:
        session.rollback()
        session.close()
