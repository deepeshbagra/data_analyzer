"""The object-store interface and the key layout. No provider SDK here.

The key layout is the interesting part of this module.
"""

from __future__ import annotations

import uuid
from typing import BinaryIO, Protocol

#: File extensions by mime, so an object downloaded out of the bucket by a human
#: opens in the right application. Not consulted by any code path -- the mime on
#: the ``source_file`` row is authoritative.
EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
}


class StorageError(RuntimeError):
    """The object store could not complete an operation.

    Distinct from the ingest errors: those mean the *file* is wrong, this means
    the *infrastructure* is. Only this one is worth retrying.
    """


class ObjectStore(Protocol):
    """Everything the pipeline needs from object storage.

    Kept deliberately small. A wider interface would tempt callers into
    provider-shaped behaviour (multipart handles, S3 event notifications) that
    the next adapter would have to emulate.
    """

    def put(
        self,
        key: str,
        stream: BinaryIO,
        *,
        content_type: str,
        content_length: int,
    ) -> None:
        """Store ``stream`` at ``key``, overwriting any existing object."""
        ...

    def get(self, key: str) -> bytes:
        """Whole object as bytes. Raises ``StorageError`` if absent."""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def presigned_get_url(self, key: str, *, expires_in: int) -> str:
        """A time-limited read URL, for the review UI's document viewer.

        Presigned rather than proxied so page images do not travel through the
        API process, and time-limited so a URL that leaks into a log or a
        browser history expires on its own.
        """
        ...


def storage_key(tenant_id: uuid.UUID, sha256: str, mime: str) -> str:
    """Where a file lives: ``{tenant_id}/raw/{sha256}{ext}``.

    Two properties, both deliberate:

    **Tenant-prefixed.** Isolation in this system is enforced by Postgres, and
    object storage is outside Postgres. The prefix is what makes a per-tenant
    bucket policy expressible in Phase 4, and what keeps two tenants who upload
    the same file from sharing one object -- which would make a deletion
    request from one tenant destroy the other's evidence.

    **Content-addressed.** The key is derived from the bytes, so writing the
    same file twice writes the same object with the same content. Uploads are
    therefore idempotent at the storage layer as well as at the database layer,
    and a retry after a half-failed ingest cannot produce two objects that
    differ.

    The original filename is deliberately absent. It is attacker-controlled
    text -- ``../../etc/passwd``, a 4KB name, an embedded newline -- and here it
    would be a path. It is stored on the row, where it is data.
    """
    return f"{tenant_id}/raw/{sha256}{EXTENSIONS.get(mime, '')}"
