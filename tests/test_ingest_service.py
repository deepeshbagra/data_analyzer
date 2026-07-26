"""Ingest against a live database: dedupe, isolation, audit and the endpoint.

The isolation assertions here matter as much as the Phase 0 ones. Object
storage sits outside Postgres, so RLS protects the rows but nothing protects
the bucket except the key layout. If two tenants uploading identical bytes
shared one object, a deletion request from one would destroy the other's
evidence -- and the database would still show a perfectly healthy row.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from api.deps import TenantContext, probe_enqueuer, require_tenant
from api.deps import object_store as object_store_dependency
from api.main import app
from api.settings import Settings
from tests.conftest import PNG_BYTES, InMemoryObjectStore, TwoTenants, build_pdf
from worker.ingest.files import (
    MIME_PDF,
    MIME_PNG,
    EncryptedFileError,
    FileTooLargeError,
    UnsupportedFileTypeError,
)
from worker.ingest.service import ingest_file, probe_file
from worker.storage.base import StorageError
from worker.storage.s3 import get_object_store

pytestmark = pytest.mark.requires_db


# ---------------------------------------------------------------------------
# Storing and deduplicating
# ---------------------------------------------------------------------------


def test_a_new_file_is_stored_and_recorded(
    two_tenants: TwoTenants,
    object_store: InMemoryObjectStore,
    admin_engine: Engine,
) -> None:
    data = build_pdf(2, text="Tax Invoice INV-2026-001 Total Rs 1,23,456.78")
    result = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="invoice.pdf",
        stream=io.BytesIO(data),
        uploaded_by=two_tenants.a.user_id,
        store=object_store,
    )

    assert result.is_duplicate is False
    assert result.mime == MIME_PDF
    assert result.byte_size == len(data)
    # Content-addressed and tenant-prefixed.
    assert result.storage_key == f"{two_tenants.a.tenant_id}/raw/{result.sha256}.pdf"
    # The bytes that came back out are the bytes that went in.
    assert object_store.get(result.storage_key) == data

    with admin_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT sha256, mime, byte_size, page_count, has_text_layer, uploaded_by "
                "FROM source_file WHERE id = :id"
            ),
            {"id": result.source_file_id},
        ).one()
    assert row.sha256 == result.sha256
    assert row.mime == MIME_PDF
    assert row.byte_size == len(data)
    assert row.uploaded_by == two_tenants.a.user_id
    # Profiling has not run yet, and the columns say so rather than guessing.
    assert row.page_count is None
    assert row.has_text_layer is None


def test_the_same_bytes_twice_yield_one_row(
    two_tenants: TwoTenants,
    object_store: InMemoryObjectStore,
    admin_engine: Engine,
) -> None:
    """Re-upload is a no-op, not an error and not a second row.

    The same invoice reaching us twice -- a retry, a double-click, the vendor's
    copy and the accountant's copy -- must not become two documents, or the
    duplicate-invoice rule fires on our own ingest bug.
    """
    data = build_pdf(1, text="Tax Invoice INV-2026-001")
    first = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="invoice.pdf",
        stream=io.BytesIO(data),
        store=object_store,
    )
    second = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="invoice-copy-2.pdf",
        stream=io.BytesIO(data),
        store=object_store,
    )

    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.source_file_id == first.source_file_id

    with admin_engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM source_file WHERE tenant_id = :t AND sha256 = :s"),
            {"t": two_tenants.a.tenant_id, "s": first.sha256},
        ).scalar_one()
    assert count == 1


def test_two_tenants_uploading_identical_bytes_get_separate_objects(
    two_tenants: TwoTenants,
    object_store: InMemoryObjectStore,
) -> None:
    """The isolation property that lives outside Postgres.

    Identical content, so identical sha256 -- but the storage key is tenant
    prefixed, so the objects are separate. Sharing one object would mean a
    DPDP erasure request from tenant A silently destroying tenant B's evidence,
    with B's rows still claiming the file exists.
    """
    data = build_pdf(1, text="Tax Invoice INV-2026-001")
    a = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="invoice.pdf",
        stream=io.BytesIO(data),
        store=object_store,
    )
    b = ingest_file(
        tenant_id=two_tenants.b.tenant_id,
        filename="invoice.pdf",
        stream=io.BytesIO(data),
        store=object_store,
    )

    assert a.sha256 == b.sha256
    assert a.source_file_id != b.source_file_id
    assert a.storage_key != b.storage_key
    assert b.is_duplicate is False
    assert a.storage_key.startswith(f"{two_tenants.a.tenant_id}/")
    assert b.storage_key.startswith(f"{two_tenants.b.tenant_id}/")


def test_a_hostile_filename_cannot_reach_the_storage_key(
    two_tenants: TwoTenants,
    object_store: InMemoryObjectStore,
) -> None:
    """The filename is attacker-controlled text. The key is derived from bytes."""
    result = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="../../../../etc/passwd\n%00.pdf",
        stream=io.BytesIO(build_pdf(1)),
        store=object_store,
    )
    assert result.storage_key == f"{two_tenants.a.tenant_id}/raw/{result.sha256}.pdf"
    assert ".." not in result.storage_key


def test_the_upload_is_audited(
    two_tenants: TwoTenants, object_store: InMemoryObjectStore, admin_engine: Engine
) -> None:
    result = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="invoice.pdf",
        stream=io.BytesIO(build_pdf(1)),
        uploaded_by=two_tenants.a.user_id,
        store=object_store,
    )
    with admin_engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT actor, action, entity_type, after FROM audit_log "
                "WHERE entity_id = :id AND action = 'source_file.uploaded'"
            ),
            {"id": result.source_file_id},
        ).one()
    assert row.actor == two_tenants.a.user_id
    assert row.entity_type == "source_file"
    assert row.after["sha256"] == result.sha256
    assert row.after["storage_key"] == result.storage_key


def test_rejected_files_leave_nothing_behind(
    two_tenants: TwoTenants,
    object_store: InMemoryObjectStore,
    admin_engine: Engine,
) -> None:
    """A rejected upload must not write an object or a row.

    Validation happens before either write for exactly this reason: a half
    ingest is worse than a refused one.
    """
    with admin_engine.begin() as conn:
        before = conn.execute(
            text("SELECT count(*) FROM source_file WHERE tenant_id = :t"),
            {"t": two_tenants.a.tenant_id},
        ).scalar_one()

    with pytest.raises(UnsupportedFileTypeError):
        ingest_file(
            tenant_id=two_tenants.a.tenant_id,
            filename="statement.xlsx",
            stream=io.BytesIO(b"PK\x03\x04" + b"\x00" * 64),
            store=object_store,
        )
    with pytest.raises(FileTooLargeError):
        ingest_file(
            tenant_id=two_tenants.a.tenant_id,
            filename="huge.pdf",
            stream=io.BytesIO(build_pdf(1)),
            store=object_store,
            max_bytes=10,
        )

    with admin_engine.begin() as conn:
        after = conn.execute(
            text("SELECT count(*) FROM source_file WHERE tenant_id = :t"),
            {"t": two_tenants.a.tenant_id},
        ).scalar_one()
    assert after == before
    assert object_store.objects == {}


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def test_probe_fills_in_page_count_and_text_layer(
    two_tenants: TwoTenants,
    object_store: InMemoryObjectStore,
    admin_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("worker.ingest.service.get_object_store", lambda: object_store)
    result = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="statement.pdf",
        stream=io.BytesIO(build_pdf(12, text="Statement of account for April 2026")),
        store=object_store,
    )

    assert probe_file(two_tenants.a.tenant_id, result.source_file_id) is True

    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT page_count, has_text_layer FROM source_file WHERE id = :id"),
            {"id": result.source_file_id},
        ).one()
    assert row.page_count == 12
    assert row.has_text_layer is True


def test_probe_of_a_scan_records_no_text_layer(
    two_tenants: TwoTenants,
    object_store: InMemoryObjectStore,
    admin_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("worker.ingest.service.get_object_store", lambda: object_store)
    result = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="scan.pdf",
        stream=io.BytesIO(build_pdf(4)),
        store=object_store,
    )
    assert probe_file(two_tenants.a.tenant_id, result.source_file_id) is True

    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT page_count, has_text_layer FROM source_file WHERE id = :id"),
            {"id": result.source_file_id},
        ).one()
    assert row.page_count == 4
    assert row.has_text_layer is False


def test_probe_of_an_encrypted_pdf_fails_loudly_and_leaves_the_columns_null(
    two_tenants: TwoTenants,
    object_store: InMemoryObjectStore,
    admin_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An encrypted file is stored -- we have it, the user sent it -- but its
    shape is unknown, and NULL says that honestly. The reason goes to the audit
    trail so a human can be told to send an unlocked copy."""
    monkeypatch.setattr("worker.ingest.service.get_object_store", lambda: object_store)
    result = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="locked.pdf",
        stream=io.BytesIO(build_pdf(1, text="Statement", password="secret")),
        store=object_store,
    )

    assert probe_file(two_tenants.a.tenant_id, result.source_file_id) is False

    with admin_engine.begin() as conn:
        row = conn.execute(
            text("SELECT page_count, has_text_layer FROM source_file WHERE id = :id"),
            {"id": result.source_file_id},
        ).one()
        audit = conn.execute(
            text(
                "SELECT after FROM audit_log WHERE entity_id = :id "
                "AND action = 'source_file.probe_failed'"
            ),
            {"id": result.source_file_id},
        ).one()
    assert row.page_count is None
    assert row.has_text_layer is None
    assert audit.after["error"] == EncryptedFileError.__name__


def test_probing_another_tenants_file_finds_nothing(
    two_tenants: TwoTenants,
    object_store: InMemoryObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RLS, from the worker's side. A task with the wrong tenant sees no row.

    It fails closed and says nothing about whether the id exists elsewhere.
    """
    monkeypatch.setattr("worker.ingest.service.get_object_store", lambda: object_store)
    result = ingest_file(
        tenant_id=two_tenants.a.tenant_id,
        filename="invoice.pdf",
        stream=io.BytesIO(build_pdf(1, text="Tax Invoice")),
        store=object_store,
    )
    assert probe_file(two_tenants.b.tenant_id, result.source_file_id) is False


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(
    two_tenants: TwoTenants, object_store: InMemoryObjectStore
) -> Iterator[tuple[TestClient, list[tuple[uuid.UUID, uuid.UUID]]]]:
    """A client wired to the in-memory store, with the Celery hop captured."""
    enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []

    app.dependency_overrides[object_store_dependency] = lambda: object_store
    app.dependency_overrides[probe_enqueuer] = lambda: (
        lambda tenant_id, source_file_id: enqueued.append((tenant_id, source_file_id))
    )
    with TestClient(app) as test_client:
        yield test_client, enqueued
    app.dependency_overrides.clear()


def _headers(tenant: TwoTenants) -> dict[str, str]:
    return {"X-Tenant-Id": str(tenant.a.tenant_id), "X-User-Id": str(tenant.a.user_id)}


def test_post_uploads_creates_and_then_deduplicates(
    client: tuple[TestClient, list[tuple[uuid.UUID, uuid.UUID]]],
    two_tenants: TwoTenants,
) -> None:
    test_client, enqueued = client
    data = build_pdf(1, text="Tax Invoice INV-2026-001")

    created = test_client.post(
        "/uploads",
        files={"file": ("invoice.pdf", data, "application/pdf")},
        headers=_headers(two_tenants),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["is_duplicate"] is False
    assert body["mime"] == MIME_PDF
    assert enqueued == [(two_tenants.a.tenant_id, uuid.UUID(body["source_file_id"]))]

    again = test_client.post(
        "/uploads",
        files={"file": ("invoice.pdf", data, "application/pdf")},
        headers=_headers(two_tenants),
    )
    # 200, not 201: nothing was created, and a client retrying after a timeout
    # needs to be able to tell the difference.
    assert again.status_code == 200
    assert again.json()["is_duplicate"] is True
    assert again.json()["source_file_id"] == body["source_file_id"]
    # No second probe queued for a file that is already known.
    assert len(enqueued) == 1


def test_the_declared_content_type_is_ignored(
    client: tuple[TestClient, list[tuple[uuid.UUID, uuid.UUID]]],
    two_tenants: TwoTenants,
) -> None:
    """A PNG announced as a PDF, with a .pdf name, is stored as a PNG.

    The mime chooses the extraction path, so if the client could set it the
    client could choose which of our code runs on their bytes.
    """
    test_client, _ = client
    response = test_client.post(
        "/uploads",
        files={"file": ("invoice.pdf", PNG_BYTES, "application/pdf")},
        headers=_headers(two_tenants),
    )
    assert response.status_code == 201
    assert response.json()["mime"] == MIME_PNG
    assert response.json()["storage_key"].endswith(".png")


@pytest.mark.parametrize(
    ("content", "expected_status"),
    [
        (b"PK\x03\x04" + b"\x00" * 64, 415),  # xlsx
        (b"", 400),  # empty
    ],
)
def test_bad_uploads_are_refused(
    client: tuple[TestClient, list[tuple[uuid.UUID, uuid.UUID]]],
    two_tenants: TwoTenants,
    content: bytes,
    expected_status: int,
) -> None:
    test_client, enqueued = client
    response = test_client.post(
        "/uploads",
        files={"file": ("thing.bin", content, "application/octet-stream")},
        headers=_headers(two_tenants),
    )
    assert response.status_code == expected_status
    assert enqueued == []


def test_upload_without_a_tenant_header_is_refused(
    client: tuple[TestClient, list[tuple[uuid.UUID, uuid.UUID]]],
) -> None:
    test_client, _ = client
    response = test_client.post(
        "/uploads",
        files={"file": ("invoice.pdf", build_pdf(1), "application/pdf")},
    )
    assert response.status_code == 400


def test_header_tenancy_is_disabled_outside_local_and_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Phase 1 stopgap must fail closed if it ever reaches production.

    A temporary auth bypass that keeps working everywhere is how a temporary
    auth bypass becomes permanent. This one returns 503 instead.
    """

    def production_settings(*_args: Any, **_kwargs: Any) -> Settings:
        return Settings.model_construct(environment="production")

    monkeypatch.setattr("api.deps.get_settings", production_settings)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/uploads",
            files={"file": ("invoice.pdf", build_pdf(1), "application/pdf")},
            headers={"X-Tenant-Id": str(uuid.uuid4())},
        )
    assert response.status_code == 503
    assert "Phase 4" in response.json()["detail"]


def test_tenant_context_is_what_the_route_receives() -> None:
    """Guards the dependency's contract independently of any route."""
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    context = require_tenant(x_tenant_id=tenant_id, x_user_id=user_id)
    assert context == TenantContext(tenant_id=tenant_id, user_id=user_id)


# ---------------------------------------------------------------------------
# The real adapter
# ---------------------------------------------------------------------------


def test_s3_adapter_round_trips_against_minio() -> None:
    """One test that actually speaks S3, because the in-memory store cannot
    catch what this adapter gets wrong.

    Everything else here injects ``InMemoryObjectStore``, which is the right
    trade for speed -- but it means path-style addressing, ``ContentLength``,
    the 404-vs-error distinction in ``exists`` and presigning are otherwise
    exercised by nothing. Those are exactly the details that differ between
    MinIO and S3.
    """
    store = get_object_store()
    key = f"smoke-test/{uuid.uuid4()}.pdf"
    data = build_pdf(1, text="Round trip")

    assert store.exists(key) is False
    store.put(key, io.BytesIO(data), content_type=MIME_PDF, content_length=len(data))
    try:
        assert store.exists(key) is True
        assert store.get(key) == data
        url = store.presigned_get_url(key, expires_in=60)
        assert key in url
        assert "X-Amz-Signature" in url
    finally:
        store.delete(key)
    assert store.exists(key) is False


def test_s3_adapter_translates_a_missing_object_into_storage_error() -> None:
    """No caller should ever have to catch a botocore exception."""
    with pytest.raises(StorageError):
        get_object_store().get(f"smoke-test/definitely-absent-{uuid.uuid4()}")
