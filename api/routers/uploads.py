"""Upload endpoint.

``POST /uploads`` is idempotent by content: posting the same bytes twice
returns 200 and the existing ``source_file`` rather than creating a second one.
That is what makes a client retry safe, and it is why the endpoint does not
need any client-supplied idempotency key -- the file *is* the key.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from api.deps import EnqueuerDep, StoreDep, TenantDep
from api.settings import get_settings
from worker.ingest.files import (
    EmptyFileError,
    FileTooLargeError,
    IngestError,
    UnsupportedFileTypeError,
)
from worker.ingest.service import ingest_file
from worker.storage.base import StorageError

router = APIRouter(tags=["uploads"])


class UploadResponse(BaseModel):
    """The stored file, whether it was stored now or already existed."""

    source_file_id: uuid.UUID
    sha256: str = Field(description="SHA-256 of the raw bytes; the dedupe key.")
    mime: str = Field(description="Sniffed from the content, not the request.")
    byte_size: int
    storage_key: str
    is_duplicate: bool = Field(
        description="True when this tenant had already uploaded these exact bytes."
    )


@router.post(
    "/uploads",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document",
    responses={
        200: {"description": "These bytes were already stored for this tenant."},
        413: {"description": "Larger than the configured limit."},
        415: {"description": "Not a PDF, PNG, JPEG or TIFF."},
    },
)
def create_upload(
    response: Response,
    tenant: TenantDep,
    store: StoreDep,
    enqueue: EnqueuerDep,
    file: UploadFile = File(description="The document. PDF, PNG, JPEG or TIFF."),  # noqa: B008
) -> UploadResponse:
    """Store a document and record it against the tenant.

    The declared ``Content-Type`` and the filename are not consulted when
    deciding what the file is -- only its bytes are. See
    ``worker.ingest.files``.
    """
    settings = get_settings()
    try:
        result = ingest_file(
            tenant_id=tenant.tenant_id,
            filename=file.filename or "unnamed",
            stream=file.file,
            uploaded_by=tenant.user_id,
            store=store,
            max_bytes=settings.max_upload_bytes,
        )
    except FileTooLargeError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc)) from exc
    except EmptyFileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except IngestError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except StorageError as exc:
        # Infrastructure, not the file. 503 tells the client to retry, and a
        # retry is safe because ingest is idempotent by content.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "object storage is unavailable"
        ) from exc

    if result.is_duplicate:
        # Nothing was created, so 201 would be a lie -- and a client that
        # retries after a timeout needs to be able to tell the difference.
        response.status_code = status.HTTP_200_OK
    else:
        enqueue(tenant.tenant_id, result.source_file_id)

    return UploadResponse(
        source_file_id=result.source_file_id,
        sha256=result.sha256,
        mime=result.mime,
        byte_size=result.byte_size,
        storage_key=result.storage_key,
        is_duplicate=result.is_duplicate,
    )
