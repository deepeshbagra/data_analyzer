"""Turning an upload into a ``source_file`` row, exactly once.

The ordering in :func:`ingest_file` is the substance of this module. Two
failure modes are possible when a write spans an object store and a database,
and they are not equally bad:

* **Object stored, row missing.** An orphaned blob. Invisible, harmless, and
  reclaimable by a sweep. Re-uploading the same file produces the same
  content-addressed key and the same bytes, so the retry heals it.
* **Row stored, object missing.** A ``source_file`` the pipeline will try to
  extract and cannot. It fails at extraction time, far from the cause, and the
  row claims a file exists that does not.

So the object is written first, and the row second. The window between them
leaks storage rather than integrity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.db.session import tenant_session
from api.models.business import AuditLog, SourceFile
from api.settings import get_settings
from worker.ingest.files import FileIdentity, identify, profile
from worker.storage.base import ObjectStore, storage_key
from worker.storage.s3 import get_object_store

#: ``original_filename`` is display text, not a path (the storage key is
#: content-addressed). Truncated only so a pathological name cannot bloat a row.
MAX_FILENAME_LENGTH = 255


@dataclass(frozen=True)
class IngestResult:
    """What ingest did, in terms the caller can act on.

    ``is_duplicate`` is not an error. Re-uploading a file the tenant already
    has is the expected outcome of a retry, a double-click, or the same invoice
    arriving by two routes, and the right response is to point at the existing
    row.
    """

    source_file_id: uuid.UUID
    sha256: str
    mime: str
    byte_size: int
    storage_key: str
    is_duplicate: bool


def ingest_file(
    *,
    tenant_id: uuid.UUID,
    filename: str,
    stream: BinaryIO,
    uploaded_by: uuid.UUID | None = None,
    store: ObjectStore | None = None,
    max_bytes: int | None = None,
) -> IngestResult:
    """Store an uploaded file and record it, or return the existing record.

    Args:
        tenant_id: owning tenant. Every read and write below runs inside a
            ``tenant_session``, so RLS applies to all of it.
        filename: as supplied by the client. Data, never a path.
        stream: the file, positioned at the start.
        uploaded_by: acting user, recorded on the row and in the audit trail.
        store: injectable for tests. Defaults to the configured S3 adapter.
        max_bytes: injectable size limit. Defaults to settings.

    Raises:
        IngestError: the file is empty, too large, or an unsupported type.
        StorageError: the object store is unreachable. Worth retrying; the
            others are not.
    """
    settings = get_settings()
    store = store if store is not None else get_object_store()
    limit = max_bytes if max_bytes is not None else settings.max_upload_bytes

    identity = identify(stream, max_bytes=limit)
    key = storage_key(tenant_id, identity.sha256, identity.mime)

    existing = _find_by_sha256(tenant_id, identity.sha256)
    if existing is not None:
        return existing

    store.put(
        key,
        stream,
        content_type=identity.mime,
        content_length=identity.byte_size,
    )

    try:
        return _record(
            tenant_id=tenant_id,
            identity=identity,
            key=key,
            filename=filename[:MAX_FILENAME_LENGTH],
            uploaded_by=uploaded_by,
        )
    except IntegrityError:
        # Two uploads of the same bytes in flight at once. The unique constraint
        # on (tenant_id, sha256) is what actually enforces "once"; the SELECT
        # above is only a fast path, and treating it as the guarantee would make
        # this a race rather than a check.
        duplicate = _find_by_sha256(tenant_id, identity.sha256)
        if duplicate is None:
            raise
        return duplicate


def probe_file(tenant_id: uuid.UUID, source_file_id: uuid.UUID) -> bool:
    """Fill in ``page_count`` and ``has_text_layer`` for a stored file.

    Split out of :func:`ingest_file` and run on the worker because it is the
    slow part: profiling a 500-page statement means parsing the whole PDF, and
    an HTTP upload should not wait for it. The columns are nullable precisely
    so this can happen afterwards.

    Returns:
        True if the row was updated, False if the file could not be profiled
        (encrypted or damaged), in which case the columns stay NULL and the
        reason is written to the audit trail.
    """
    store = get_object_store()

    with tenant_session(tenant_id) as session:
        row = session.scalar(
            select(SourceFile).where(
                SourceFile.tenant_id == tenant_id,
                SourceFile.id == source_file_id,
            )
        )
        if row is None:
            # Either the row is gone or this session is scoped to the wrong
            # tenant. RLS makes those indistinguishable from here, which is the
            # intended behaviour: fail closed, say nothing about other tenants.
            return False
        key, mime = row.storage_key, row.mime

    data = store.get(key)

    try:
        result = profile(data, mime)
    except ValueError as exc:
        with tenant_session(tenant_id) as session:
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    action="source_file.probe_failed",
                    entity_type="source_file",
                    entity_id=source_file_id,
                    after={"error": type(exc).__name__, "detail": str(exc)},
                )
            )
        return False

    with tenant_session(tenant_id) as session:
        row = session.scalar(
            select(SourceFile).where(
                SourceFile.tenant_id == tenant_id,
                SourceFile.id == source_file_id,
            )
        )
        if row is None:
            return False
        row.page_count = result.page_count
        row.has_text_layer = result.has_text_layer
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                action="source_file.profiled",
                entity_type="source_file",
                entity_id=source_file_id,
                after={
                    "page_count": result.page_count,
                    "has_text_layer": result.has_text_layer,
                    "pages_sampled": result.pages_sampled,
                    "pages_with_text": result.pages_with_text,
                },
            )
        )
    return True


def _find_by_sha256(tenant_id: uuid.UUID, sha256: str) -> IngestResult | None:
    with tenant_session(tenant_id) as session:
        # RLS already confines this to the tenant; the explicit predicate is
        # the project rule, and it keeps the index lookup tenant-leading.
        row = session.scalar(
            select(SourceFile).where(
                SourceFile.tenant_id == tenant_id,
                SourceFile.sha256 == sha256,
            )
        )
        if row is None:
            return None
        return IngestResult(
            source_file_id=row.id,
            sha256=row.sha256,
            mime=row.mime,
            byte_size=row.byte_size,
            storage_key=row.storage_key,
            is_duplicate=True,
        )


def _record(
    *,
    tenant_id: uuid.UUID,
    identity: FileIdentity,
    key: str,
    filename: str,
    uploaded_by: uuid.UUID | None,
) -> IngestResult:
    with tenant_session(tenant_id, user_id=uploaded_by) as session:
        row = SourceFile(
            tenant_id=tenant_id,
            sha256=identity.sha256,
            storage_key=key,
            original_filename=filename,
            mime=identity.mime,
            byte_size=identity.byte_size,
            uploaded_by=uploaded_by,
        )
        session.add(row)
        # Flush rather than commit: the audit entry needs the generated id, and
        # the row and its audit record must land in the same transaction or the
        # trail has gaps exactly where a failure happened.
        session.flush()
        source_file_id = row.id
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=uploaded_by,
                action="source_file.uploaded",
                entity_type="source_file",
                entity_id=source_file_id,
                after={
                    "original_filename": filename,
                    "sha256": identity.sha256,
                    "mime": identity.mime,
                    "byte_size": identity.byte_size,
                    "storage_key": key,
                },
            )
        )

    return IngestResult(
        source_file_id=source_file_id,
        sha256=identity.sha256,
        mime=identity.mime,
        byte_size=identity.byte_size,
        storage_key=key,
        is_duplicate=False,
    )
