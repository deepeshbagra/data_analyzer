"""Enumerations backed by native Postgres enum types.

Native enums (rather than text + CHECK) so the database rejects a bad value
even when a row arrives from psql, a migration, or the Tally sync agent rather
than from application code. Adding a value later is ``ALTER TYPE ... ADD
VALUE``, which Postgres 16 permits inside a transaction.
"""

from __future__ import annotations

from enum import StrEnum


class MembershipRole(StrEnum):
    """Roles are ordered from most to least privileged; see Phase 4."""

    OWNER = "owner"
    ACCOUNTANT = "accountant"
    REVIEWER = "reviewer"
    READ_ONLY = "read_only"


class PartyKind(StrEnum):
    VENDOR = "vendor"
    CUSTOMER = "customer"
    BOTH = "both"
    EMPLOYEE = "employee"  # needed by the "vendor bank == employee bank" rule


class DocumentType(StrEnum):
    PURCHASE_INVOICE = "purchase_invoice"
    SALES_INVOICE = "sales_invoice"
    RECEIPT = "receipt"
    EXPENSE = "expense"
    LEDGER = "ledger"
    BANK_STATEMENT = "bank_statement"
    CREDIT_NOTE = "credit_note"
    DEBIT_NOTE = "debit_note"
    DELIVERY_NOTE = "delivery_note"
    PURCHASE_ORDER = "purchase_order"
    UNKNOWN = "unknown"  # classifier declined; goes straight to review


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"
    FAILED = "failed"


class EntityType(StrEnum):
    """Targets of a polymorphic reference from ``link`` or ``finding``."""

    DOCUMENT = "document"
    LINE_ITEM = "line_item"
    LEDGER_ENTRY = "ledger_entry"
    BANK_TXN = "bank_txn"
    PARTY = "party"
    SOURCE_FILE = "source_file"


class LinkType(StrEnum):
    """Named relationships in the reconciliation graph (Phase 2)."""

    BANK_TXN_SETTLES_DOCUMENT = "bank_txn_settles_document"
    LEDGER_ENTRY_FOR_DOCUMENT = "ledger_entry_for_document"
    BANK_TXN_TO_LEDGER_ENTRY = "bank_txn_to_ledger_entry"
    DOCUMENT_FULFILS_DOCUMENT = "document_fulfils_document"  # PO/GRN -> invoice
    DOCUMENT_OFFSETS_DOCUMENT = "document_offsets_document"  # credit note -> invoice
    GSTR2B_MATCHES_DOCUMENT = "gstr2b_matches_document"
    DUPLICATE_OF = "duplicate_of"


class LinkStatus(StrEnum):
    PROPOSED = "proposed"
    AUTO_ACCEPTED = "auto_accepted"  # above the tenant's auto-accept threshold
    ACCEPTED = "accepted"  # a human said yes
    REJECTED = "rejected"


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


class TxnDirection(StrEnum):
    """Bank transaction direction. ``bank_txn.amount`` is always positive.

    Storing a signed amount invites sign-convention bugs across statement
    formats; direction is explicit instead.
    """

    DEBIT = "debit"  # money out
    CREDIT = "credit"  # money in


# Mapping from enum class to the Postgres type name used in migrations.
PG_ENUM_NAMES: dict[type[StrEnum], str] = {
    MembershipRole: "membership_role",
    PartyKind: "party_kind",
    DocumentType: "document_type",
    ExtractionStatus: "extraction_status",
    EntityType: "entity_type",
    LinkType: "link_type",
    LinkStatus: "link_status",
    FindingSeverity: "finding_severity",
    FindingStatus: "finding_status",
    TxnDirection: "txn_direction",
}
