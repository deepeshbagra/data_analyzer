"""Shared FastAPI dependencies.

The tenant dependency here is a **Phase 1 stopgap**. Real authentication is
Phase 4 (DECISIONS #5: self-hosted JWT with a `membership` join). Until then
the tenant is taken from a request header, which means any caller can name any
tenant.

That is only tolerable because it fails closed: :func:`require_tenant` refuses
to run at all outside ``local`` and ``test``. A deployment that reaches staging
or production with this still in place returns 503 on every request rather than
quietly serving one tenant's data to another. The alternative -- a header
stopgap that works everywhere and is "obviously temporary" -- is exactly the
kind of thing that survives to production, and the failure mode is a
cross-tenant breach.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from api.settings import get_settings
from worker.storage.base import ObjectStore
from worker.storage.s3 import get_object_store

#: Environments where header-supplied tenancy is permitted. Nothing else.
UNAUTHENTICATED_TENANT_ENVIRONMENTS = frozenset({"local", "test"})

#: (tenant_id, source_file_id) -> None. Queues the post-upload probe.
ProbeEnqueuer = Callable[[uuid.UUID, uuid.UUID], None]


@dataclass(frozen=True)
class TenantContext:
    """Who is acting, and on whose data."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID | None


def require_tenant(
    x_tenant_id: Annotated[uuid.UUID | None, Header()] = None,
    x_user_id: Annotated[uuid.UUID | None, Header()] = None,
) -> TenantContext:
    """Resolve the acting tenant, or refuse.

    Raises:
        HTTPException: 503 outside local/test, because header-supplied tenancy
            is not an access control. 400 when the header is absent.
    """
    settings = get_settings()
    if settings.environment not in UNAUTHENTICATED_TENANT_ENVIRONMENTS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "header-supplied tenant selection is disabled outside local and test; "
                "authentication lands in Phase 4"
            ),
        )
    if x_tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Tenant-Id is required until Phase 4 authentication lands",
        )
    return TenantContext(tenant_id=x_tenant_id, user_id=x_user_id)


def object_store() -> ObjectStore:
    """The configured object store, as a dependency so tests can override it."""
    return get_object_store()


def enqueue_probe(tenant_id: uuid.UUID, source_file_id: uuid.UUID) -> None:
    """Queue the page-count and text-layer probe for a freshly stored file.

    Imported lazily inside the function: pulling the task module in at import
    time would make every API process construct a Celery task registry it does
    not otherwise need, and would couple `api` startup to `worker` import
    health.
    """
    from worker.ingest.tasks import probe_source_file  # noqa: PLC0415

    probe_source_file.delay(str(tenant_id), str(source_file_id))


def probe_enqueuer() -> ProbeEnqueuer:
    """Indirection so a test can assert enqueueing without running a broker."""
    return enqueue_probe


TenantDep = Annotated[TenantContext, Depends(require_tenant)]
StoreDep = Annotated[ObjectStore, Depends(object_store)]
EnqueuerDep = Annotated[ProbeEnqueuer, Depends(probe_enqueuer)]
