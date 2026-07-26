"""The canonical business schema.

This module is the contract referred to by architecture principle 4: all
feature code reads these tables and never re-reads the PDF. Two traceability
invariants are encoded structurally rather than by convention:

* ``source_file`` + ``document.page_range`` + ``document.field_confidence``
  (which stores per-field bounding boxes alongside scores) mean any extracted
  value can be traced back to a file, page and region.
* ``link.evidence`` and ``finding.evidence`` are non-null JSONB carrying the
  per-signal breakdown, so no reconciliation or finding can exist without a
  record of why it exists.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INT4RANGE, JSONB, Range
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db.base import (
    Base,
    Confidence,
    Money,
    Rate,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKey,
)
from api.models.enums import (
    DocumentType,
    EntityType,
    ExtractionStatus,
    FindingSeverity,
    FindingStatus,
    LinkStatus,
    LinkType,
    PartyKind,
    TxnDirection,
)
from api.models.types import pg_enum

# ---------------------------------------------------------------------------
# Parties
# ---------------------------------------------------------------------------


class Party(Base, UUIDPrimaryKey, TenantMixin, TimestampMixin):
    """A counterparty: vendor, customer, or both.

    ``aliases`` holds the name variants seen in the wild -- transliteration
    differences, missing suffixes, bank-narration abbreviations. Phase 2's
    name-similarity scorer reads it, so a human correction on one document
    improves matching on every later one.
    """

    __tablename__ = "party"

    kind: Mapped[PartyKind] = mapped_column(pg_enum(PartyKind))
    legal_name: Mapped[str]
    display_name: Mapped[str]

    gstin: Mapped[str | None]
    pan: Mapped[str | None]

    #: list[str] of observed name variants.
    aliases: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    #: list[dict] of {account_number_hash, ifsc, bank_name, last4}. Hashed, not
    #: plaintext -- the "vendor bank account matches an employee account" rule
    #: only needs equality, so storing the number itself is needless exposure.
    bank_accounts: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )

    payment_terms_days: Mapped[int | None]
    is_first_time: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    __table_args__ = (
        Index("ix_party_tenant_id_gstin", "tenant_id", "gstin"),
        Index("ix_party_tenant_id_pan", "tenant_id", "pan"),
        # Trigram index: Phase 2 blocking must be index-backed, and fuzzy name
        # candidates come from here rather than a sequential scan.
        Index(
            "ix_party_display_name_trgm",
            "display_name",
            postgresql_using="gin",
            postgresql_ops={"display_name": "gin_trgm_ops"},
        ),
        CheckConstraint("gstin IS NULL OR char_length(gstin) = 15", name="gstin_length"),
        CheckConstraint("pan IS NULL OR char_length(pan) = 10", name="pan_length"),
    )


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


class SourceFile(Base, UUIDPrimaryKey, TenantMixin, TimestampMixin):
    """An uploaded artefact. The root of every traceability chain."""

    __tablename__ = "source_file"

    #: SHA-256 of the raw bytes. Unique per tenant: re-uploading the same file
    #: is a no-op, and the extraction cache is keyed on this.
    sha256: Mapped[str]
    #: Object storage key, always prefixed with the tenant id.
    storage_key: Mapped[str] = mapped_column(unique=True)
    original_filename: Mapped[str]
    mime: Mapped[str]
    byte_size: Mapped[int] = mapped_column(BigInteger)

    page_count: Mapped[int | None]
    #: NULL until detection runs. False means scanned -> OCR/VLM path.
    has_text_layer: Mapped[bool | None]

    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )

    documents: Mapped[list[Document]] = relationship(back_populates="source_file")

    __table_args__ = (
        UniqueConstraint("tenant_id", "sha256", name="uq_source_file_tenant_id_sha256"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("page_count IS NULL OR page_count > 0", name="page_count_positive"),
    )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class Document(Base, UUIDPrimaryKey, TenantMixin, TimestampMixin):
    """One logical business document.

    A 20-invoice scanned batch is one ``source_file`` and twenty ``document``
    rows, each owning a disjoint ``page_range``.
    """

    __tablename__ = "document"

    type: Mapped[DocumentType] = mapped_column(pg_enum(DocumentType))
    number: Mapped[str | None]
    doc_date: Mapped[dt.date | None] = mapped_column(Date)
    due_date: Mapped[dt.date | None] = mapped_column(Date)

    party_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("party.id", ondelete="RESTRICT"), index=True
    )

    currency: Mapped[str] = mapped_column(Text, default="INR", server_default="INR")
    subtotal: Mapped[Money | None]
    tax_total: Mapped[Money | None]
    grand_total: Mapped[Money | None]

    source_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_file.id", ondelete="RESTRICT"), index=True
    )
    #: Inclusive-exclusive page span within the source file, 1-based. A range
    #: rather than two columns so "which document owns page 7?" is a
    #: containment query the splitter and review UI both use.
    page_range: Mapped[Range[int]] = mapped_column(INT4RANGE)

    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        pg_enum(ExtractionStatus),
        default=ExtractionStatus.PENDING,
        server_default=ExtractionStatus.PENDING.value,
    )
    #: Per-field {score, page, bbox, adapter} -- the field-level half of
    #: principle 3. Reviewed fields record the reviewer instead of a score.
    field_confidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )

    # Reproducibility (Phase 1 requirement). On the row, not just in logs:
    # logs get rotated, and we need to answer "which model produced this
    # number" for any row, years later.
    extractor_adapter: Mapped[str | None]
    extractor_model_version: Mapped[str | None]
    prompt_hash: Mapped[str | None]
    extracted_at: Mapped[dt.datetime | None]

    source_file: Mapped[SourceFile] = relationship(back_populates="documents")
    party: Mapped[Party | None] = relationship()
    line_items: Mapped[list[LineItem]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="LineItem.line_no",
    )

    __table_args__ = (
        # NOT unique, deliberately. The findings engine has to detect repeated
        # invoice numbers and duplicate documents, which means the schema must
        # permit them to exist.
        Index("ix_document_tenant_id_party_id_number", "tenant_id", "party_id", "number"),
        Index("ix_document_tenant_id_type_doc_date", "tenant_id", "type", "doc_date"),
        Index("ix_document_tenant_id_grand_total", "tenant_id", "grand_total"),
        Index("ix_document_tenant_id_extraction_status", "tenant_id", "extraction_status"),
        # Supports the page-ownership lookup described above.
        Index(
            "ix_document_source_file_id_page_range",
            "source_file_id",
            "page_range",
            postgresql_using="gist",
        ),
        CheckConstraint("char_length(currency) = 3", name="currency_iso4217"),
        CheckConstraint("NOT isempty(page_range)", name="page_range_non_empty"),
        CheckConstraint("lower(page_range) >= 1", name="page_range_one_based"),
    )


class LineItem(Base, UUIDPrimaryKey, TenantMixin, TimestampMixin):
    """A single line of a document.

    ``tenant_id`` is denormalised from the parent document purely so the RLS
    policy binds to a column on this table instead of requiring a join.
    """

    __tablename__ = "line_item"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int]

    description: Mapped[str | None]
    hsn_sku: Mapped[str | None]
    qty: Mapped[Money | None]
    rate: Mapped[Money | None]
    tax_rate: Mapped[Rate | None]
    amount: Mapped[Money | None]

    document: Mapped[Document] = relationship(back_populates="line_items")

    __table_args__ = (
        UniqueConstraint("document_id", "line_no", name="uq_line_item_document_id_line_no"),
        # Supports the supplier-price-increase and negative-margin rules.
        Index("ix_line_item_tenant_id_hsn_sku", "tenant_id", "hsn_sku"),
        CheckConstraint("line_no >= 1", name="line_no_positive"),
    )


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


class LedgerEntry(Base, UUIDPrimaryKey, TenantMixin, TimestampMixin):
    """One side of a double-entry posting."""

    __tablename__ = "ledger_entry"

    account: Mapped[str]
    debit: Mapped[Money] = mapped_column(default=Decimal("0"), server_default="0")
    credit: Mapped[Money] = mapped_column(default=Decimal("0"), server_default="0")
    entry_date: Mapped[dt.date] = mapped_column(Date)

    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL"), index=True
    )
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL")
    )
    party_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("party.id", ondelete="SET NULL"))

    narration: Mapped[str | None]
    voucher_type: Mapped[str | None]
    voucher_no: Mapped[str | None]
    #: External system identity (e.g. Tally GUID) for idempotent re-sync.
    external_id: Mapped[str | None]
    #: When the entry was recorded, as opposed to its effective date. The
    #: backdated-entry rule compares the two.
    recorded_at: Mapped[dt.datetime | None]

    __table_args__ = (
        Index("ix_ledger_entry_tenant_id_account_entry_date", "tenant_id", "account", "entry_date"),
        UniqueConstraint("tenant_id", "external_id", name="uq_ledger_entry_tenant_id_external_id"),
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts_non_negative"),
        # A ledger line is one-sided. Both non-zero means the importer merged
        # two postings, which we want to fail loudly rather than reconcile.
        CheckConstraint("debit = 0 OR credit = 0", name="single_sided"),
    )


# ---------------------------------------------------------------------------
# Banking
# ---------------------------------------------------------------------------


class BankAccount(Base, UUIDPrimaryKey, TenantMixin, TimestampMixin):
    """A bank account.

    Not in the original schema list, but ``bank_txn.account_id`` needs a
    referent. Also holds the account numbers the employee-collusion rule
    compares against, and is the attachment point for the Phase 4 Account
    Aggregator feed.
    """

    __tablename__ = "bank_account"

    bank_name: Mapped[str | None]
    #: Display-safe form, e.g. "XXXXXX4321".
    account_number_masked: Mapped[str | None]
    #: Salted hash. Equality comparisons only -- we never need the plaintext.
    account_number_hash: Mapped[str | None]
    ifsc: Mapped[str | None]
    currency: Mapped[str] = mapped_column(Text, default="INR", server_default="INR")

    #: True for the tenant's own accounts, False for a counterparty's.
    is_own: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    party_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("party.id", ondelete="SET NULL"))

    transactions: Mapped[list[BankTxn]] = relationship(back_populates="account")

    __table_args__ = (
        Index("ix_bank_account_tenant_id_account_number_hash", "tenant_id", "account_number_hash"),
        CheckConstraint("char_length(currency) = 3", name="currency_iso4217"),
    )


class BankTxn(Base, UUIDPrimaryKey, TenantMixin, TimestampMixin):
    """A single bank line. ``amount`` is always positive; see TxnDirection."""

    __tablename__ = "bank_txn"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bank_account.id", ondelete="CASCADE"), index=True
    )
    txn_date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[Money]
    direction: Mapped[TxnDirection] = mapped_column(pg_enum(TxnDirection))
    narration: Mapped[str | None]
    balance: Mapped[Money | None]

    #: Best guess from narration parsing. Nullable and never authoritative --
    #: matching treats it as one signal among several.
    inferred_party_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("party.id", ondelete="SET NULL"), index=True
    )
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL")
    )
    #: Bank/AA-provided reference. Makes re-import idempotent.
    external_id: Mapped[str | None]
    value_date: Mapped[dt.date | None] = mapped_column(Date)

    account: Mapped[BankAccount] = relationship(back_populates="transactions")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "account_id",
            "external_id",
            name="uq_bank_txn_tenant_id_account_id_external_id",
        ),
        # Blocking queries filter on date window and amount band.
        Index("ix_bank_txn_tenant_id_txn_date_amount", "tenant_id", "txn_date", "amount"),
        Index("ix_bank_txn_tenant_id_amount", "tenant_id", "amount"),
        # Document-number-in-narration search.
        Index(
            "ix_bank_txn_narration_trgm",
            "narration",
            postgresql_using="gin",
            postgresql_ops={"narration": "gin_trgm_ops"},
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
    )


# ---------------------------------------------------------------------------
# Reconciliation graph
# ---------------------------------------------------------------------------


class Link(Base, UUIDPrimaryKey, TenantMixin, TimestampMixin):
    """An edge in the reconciliation graph.

    Polymorphic on both ends, so there are no real foreign keys to the
    endpoints. Referential integrity for edges is the job of the Phase 2
    traversal helpers and a periodic consistency check, not the database.
    """

    __tablename__ = "link"

    from_type: Mapped[EntityType] = mapped_column(pg_enum(EntityType))
    from_id: Mapped[uuid.UUID]
    to_type: Mapped[EntityType] = mapped_column(pg_enum(EntityType))
    to_id: Mapped[uuid.UUID]
    link_type: Mapped[LinkType] = mapped_column(pg_enum(LinkType))

    #: 0..1 composite score from scoring.py.
    confidence: Mapped[Confidence]
    #: How much of the endpoints' amounts this edge accounts for. Part
    #: payments and split settlements mean this is not the document total.
    matched_amount: Mapped[Money | None]

    status: Mapped[LinkStatus] = mapped_column(
        pg_enum(LinkStatus),
        default=LinkStatus.PROPOSED,
        server_default=LinkStatus.PROPOSED.value,
    )
    #: Per-signal breakdown. Non-null: an edge without evidence is not
    #: reviewable, and every number shown to a user must be explainable.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Version of the matcher config/weights that produced this edge, so a
    #: weight change can be attributed when auto-match rate moves.
    matcher_version: Mapped[str | None]

    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    decided_at: Mapped[dt.datetime | None]

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "from_type",
            "from_id",
            "to_type",
            "to_id",
            "link_type",
            name="uq_link_endpoints",
        ),
        Index("ix_link_tenant_id_from_type_from_id", "tenant_id", "from_type", "from_id"),
        Index("ix_link_tenant_id_to_type_to_id", "tenant_id", "to_type", "to_id"),
        Index("ix_link_tenant_id_status_confidence", "tenant_id", "status", "confidence"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_unit_interval"),
        CheckConstraint("NOT (from_type = to_type AND from_id = to_id)", name="no_self_link"),
    )


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


class Finding(Base, UUIDPrimaryKey, TenantMixin, TimestampMixin):
    """A risk or anomaly raised by a rule."""

    __tablename__ = "finding"

    #: Stable identifier of the rule module, e.g. "duplicate_invoice".
    rule_id: Mapped[str]
    severity: Mapped[FindingSeverity] = mapped_column(pg_enum(FindingSeverity))
    title: Mapped[str]

    #: [{"type": "document", "id": "..."}] -- the rows this finding is about.
    entity_refs: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    #: Rule-specific proof, pointing at document/page/field. Non-null: a
    #: finding a user cannot drill into does not ship.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )

    status: Mapped[FindingStatus] = mapped_column(
        pg_enum(FindingStatus),
        default=FindingStatus.OPEN,
        server_default=FindingStatus.OPEN.value,
    )
    #: Rule-computed identity so a re-run updates rather than duplicates.
    dedupe_key: Mapped[str]
    first_seen_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())

    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[dt.datetime | None]
    resolution_note: Mapped[str | None]

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "rule_id", "dedupe_key", name="uq_finding_tenant_id_rule_id_dedupe_key"
        ),
        Index("ix_finding_tenant_id_status_severity", "tenant_id", "status", "severity"),
        Index("ix_finding_tenant_id_rule_id", "tenant_id", "rule_id"),
    )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class AuditLog(Base, TenantMixin):
    """Append-only record of everything that happened.

    Not a UUID primary key: a monotonic bigint gives cheap ordered pagination
    over what will be the largest table in the system. Migration 0002 revokes
    UPDATE and DELETE and installs a trigger that raises on either, so
    immutability is enforced by the database rather than promised by the code.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    actor: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    #: Denormalised so the trail survives user deletion.
    actor_email: Mapped[str | None]
    #: True when the actor was acting as an external advisor (CA-firm mode).
    actor_is_external: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    action: Mapped[str]
    entity_type: Mapped[str]
    entity_id: Mapped[uuid.UUID | None]

    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    request_id: Mapped[str | None]
    ip: Mapped[str | None]
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index("ix_audit_log_tenant_id_created_at", "tenant_id", "created_at"),
        Index(
            "ix_audit_log_tenant_id_entity_type_entity_id", "tenant_id", "entity_type", "entity_id"
        ),
        Index("ix_audit_log_tenant_id_actor", "tenant_id", "actor"),
    )


__all__ = [
    "AuditLog",
    "BankAccount",
    "BankTxn",
    "Document",
    "Finding",
    "LedgerEntry",
    "LineItem",
    "Link",
    "Party",
    "SourceFile",
]
