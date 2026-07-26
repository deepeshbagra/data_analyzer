"""Shared test fixtures.

Two connection identities are in play throughout the suite:

* ``admin_conn`` -- the schema owner. A superuser, therefore exempt from RLS.
  Used only to seed and tear down data that the tests then try, and fail, to
  reach across the tenant boundary.
* ``rw_engine`` -- the ``app_rw`` role the application actually uses. Every
  isolation assertion runs through this, because asserting isolation from a
  superuser connection would prove nothing.
"""

from __future__ import annotations

import base64
import io
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import BinaryIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError


def _dsn(var: str) -> str | None:
    return os.environ.get(var)


def _require_db(dsn: str | None, var: str) -> str:
    """Return the DSN or stop the run with an actionable message.

    A security test that silently skips is worse than no test, so this errors
    rather than skips when running in CI.
    """
    if dsn:
        return dsn
    message = (
        f"{var} is not set. Tenant isolation cannot be verified without a live "
        f"database. Run `.\\tasks.ps1 test` (or `make test`), which starts "
        f"Postgres and applies migrations first."
    )
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def admin_engine() -> Iterator[Engine]:
    dsn = _require_db(_dsn("DATABASE_ADMIN_URL"), "DATABASE_ADMIN_URL")
    engine = create_engine(dsn, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        message = f"Cannot reach Postgres at DATABASE_ADMIN_URL: {exc.orig}"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def rw_engine() -> Iterator[Engine]:
    """The non-superuser runtime role. RLS applies to it."""
    dsn = _require_db(_dsn("DATABASE_URL"), "DATABASE_URL")
    engine = create_engine(dsn, future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def ro_engine() -> Iterator[Engine]:
    dsn = _require_db(_dsn("DATABASE_RO_URL"), "DATABASE_RO_URL")
    engine = create_engine(dsn, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def admin_conn(admin_engine: Engine) -> Iterator[Connection]:
    with admin_engine.begin() as conn:
        yield conn


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantFixture:
    """Identifiers for one seeded tenant's rows."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    party_id: uuid.UUID
    source_file_id: uuid.UUID
    document_id: uuid.UUID
    bank_account_id: uuid.UUID
    bank_txn_id: uuid.UUID
    invoice_number: str


@dataclass(frozen=True)
class TwoTenants:
    a: TenantFixture
    b: TenantFixture


def _seed_tenant(conn: Connection, label: str, *, grand_total: Decimal) -> TenantFixture:
    suffix = uuid.uuid4().hex[:8]
    tenant_id = conn.execute(
        text("INSERT INTO tenant (name, slug) VALUES (:name, :slug) RETURNING id"),
        {"name": f"Test Tenant {label}", "slug": f"test-{label.lower()}-{suffix}"},
    ).scalar_one()

    user_id = conn.execute(
        text(
            "INSERT INTO app_user (email, password_hash, full_name) "
            "VALUES (:email, :pw, :name) RETURNING id"
        ),
        {
            "email": f"user-{label.lower()}-{suffix}@example.test",
            "pw": "$argon2id$not-a-real-hash",
            "name": f"User {label}",
        },
    ).scalar_one()

    conn.execute(
        text("INSERT INTO membership (tenant_id, user_id, role) VALUES (:t, :u, 'owner')"),
        {"t": tenant_id, "u": user_id},
    )

    party_id = conn.execute(
        text(
            "INSERT INTO party (tenant_id, kind, legal_name, display_name) "
            "VALUES (:t, 'vendor', :legal, :display) RETURNING id"
        ),
        {
            "t": tenant_id,
            "legal": f"Vendor {label} Private Limited",
            "display": f"Vendor {label}",
        },
    ).scalar_one()

    source_file_id = conn.execute(
        text(
            "INSERT INTO source_file "
            "(tenant_id, sha256, storage_key, original_filename, mime, byte_size, "
            " page_count, has_text_layer) "
            "VALUES (:t, :sha, :key, :fn, 'application/pdf', 1024, 1, true) RETURNING id"
        ),
        {
            "t": tenant_id,
            "sha": uuid.uuid4().hex * 2,
            "key": f"{tenant_id}/raw/{suffix}.pdf",
            "fn": f"invoice-{label}.pdf",
        },
    ).scalar_one()

    invoice_number = f"INV-{label}-{suffix}"
    document_id = conn.execute(
        text(
            "INSERT INTO document "
            "(tenant_id, type, number, doc_date, party_id, currency, subtotal, "
            " tax_total, grand_total, source_file_id, page_range, extraction_status) "
            "VALUES (:t, 'purchase_invoice', :num, '2026-04-15', :p, 'INR', "
            " :sub, :tax, :total, :sf, '[1,2)'::int4range, 'extracted') RETURNING id"
        ),
        {
            "t": tenant_id,
            "num": invoice_number,
            "p": party_id,
            "sub": grand_total / Decimal("1.18"),
            "tax": grand_total - (grand_total / Decimal("1.18")),
            "total": grand_total,
            "sf": source_file_id,
        },
    ).scalar_one()

    conn.execute(
        text(
            "INSERT INTO line_item "
            "(tenant_id, document_id, line_no, description, qty, rate, tax_rate, amount) "
            "VALUES (:t, :d, 1, :desc, 1, :rate, 18, :amount)"
        ),
        {
            "t": tenant_id,
            "d": document_id,
            "desc": f"Consulting services for {label}",
            "rate": grand_total / Decimal("1.18"),
            "amount": grand_total / Decimal("1.18"),
        },
    )

    bank_account_id = conn.execute(
        text(
            "INSERT INTO bank_account (tenant_id, bank_name, account_number_masked, is_own) "
            "VALUES (:t, :bank, 'XXXXXX0001', true) RETURNING id"
        ),
        {"t": tenant_id, "bank": f"Bank of {label}"},
    ).scalar_one()

    bank_txn_id = conn.execute(
        text(
            "INSERT INTO bank_txn "
            "(tenant_id, account_id, txn_date, amount, direction, narration) "
            "VALUES (:t, :acc, '2026-04-20', :amt, 'debit', :narration) RETURNING id"
        ),
        {
            "t": tenant_id,
            "acc": bank_account_id,
            "amt": grand_total,
            "narration": f"NEFT {invoice_number} VENDOR {label}",
        },
    ).scalar_one()

    conn.execute(
        text(
            "INSERT INTO audit_log (tenant_id, actor, action, entity_type, entity_id) "
            "VALUES (:t, :u, 'document.extracted', 'document', :d)"
        ),
        {"t": tenant_id, "u": user_id, "d": document_id},
    )

    return TenantFixture(
        tenant_id=tenant_id,
        user_id=user_id,
        party_id=party_id,
        source_file_id=source_file_id,
        document_id=document_id,
        bank_account_id=bank_account_id,
        bank_txn_id=bank_txn_id,
        invoice_number=invoice_number,
    )


@pytest.fixture
def two_tenants(admin_engine: Engine) -> Iterator[TwoTenants]:
    """Two fully populated tenants with deliberately similar data.

    Amounts and dates overlap so that a broken policy leaks rows that look
    plausible rather than obviously foreign -- which is how a real leak
    presents.
    """
    with admin_engine.begin() as conn:
        a = _seed_tenant(conn, "A", grand_total=Decimal("118000.0000"))
        b = _seed_tenant(conn, "B", grand_total=Decimal("118000.0000"))

    yield TwoTenants(a=a, b=b)

    # ON DELETE CASCADE from tenant removes everything seeded above.
    with admin_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM tenant WHERE id = ANY(:ids)"),
            {"ids": [a.tenant_id, b.tenant_id]},
        )
        conn.execute(
            text("DELETE FROM app_user WHERE id = ANY(:ids)"),
            {"ids": [a.user_id, b.user_id]},
        )


# ---------------------------------------------------------------------------
# Synthetic files
#
# Built rather than checked in as binary fixtures: the tests need to vary page
# count, text layer and encryption independently, and a reader cannot tell from
# a committed blob which property it was meant to exercise.
# ---------------------------------------------------------------------------


def build_pdf(pages: int = 1, *, text: str | None = None, password: str | None = None) -> bytes:
    """A minimal but genuinely valid PDF.

    ``text`` injects a real content stream with a font resource, which is what
    makes ``extract_text`` return anything -- a content stream without
    ``/Resources`` extracts as empty, so a fixture missing it would silently
    test the scanned path while claiming to test the text path.
    """
    writer = PdfWriter()
    # _add_object: pypdf exposes no public way to add an indirect object.
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for _ in range(pages):
        page = writer.add_blank_page(width=595, height=842)
        if text is None:
            continue
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 40 700 Td (" + text.encode("ascii") + b") Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
    if password is not None:
        writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


#: A 1x1 PNG. Small enough to inline, real enough to sniff and store.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class InMemoryObjectStore:
    """An ``ObjectStore`` that keeps blobs in a dict.

    Deliberately not a mock: the tests assert on what was stored and under
    which key, and the tenant prefix on that key is a real isolation property.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    def put(
        self,
        key: str,
        stream: BinaryIO,
        *,
        content_type: str,
        content_length: int,
    ) -> None:
        data = stream.read()
        if len(data) != content_length:
            raise AssertionError(f"declared {content_length} bytes, wrote {len(data)}")
        self.objects[key] = data
        self.content_types[key] = content_type

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.content_types.pop(key, None)

    def presigned_get_url(self, key: str, *, expires_in: int) -> str:
        return f"memory://{key}?expires_in={expires_in}"


@pytest.fixture
def object_store() -> InMemoryObjectStore:
    return InMemoryObjectStore()
