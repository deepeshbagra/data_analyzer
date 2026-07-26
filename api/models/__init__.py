"""Canonical schema. Importing this module registers every table on ``Base``."""

from __future__ import annotations

from api.db.base import Base, TenantMixin
from api.models.business import (
    AuditLog,
    BankAccount,
    BankTxn,
    Document,
    Finding,
    LedgerEntry,
    LineItem,
    Link,
    Party,
    SourceFile,
)
from api.models.enums import (
    DocumentType,
    EntityType,
    ExtractionStatus,
    FindingSeverity,
    FindingStatus,
    LinkStatus,
    LinkType,
    MembershipRole,
    PartyKind,
    TxnDirection,
)
from api.models.identity import AppUser, Membership, Tenant

#: Every table carrying a ``tenant_id``, derived from the mapper registry
#: rather than hand-maintained. ``tests/test_schema_rls_coverage.py`` asserts
#: each of these has RLS enabled, forced, and a policy attached -- so adding a
#: tenant-scoped table in a later phase and forgetting its policy fails CI.
TENANT_SCOPED_TABLES: tuple[str, ...] = tuple(
    sorted(
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if "tenant_id" in mapper.class_.__table__.c
    )
)

#: Tables that legitimately have no ``tenant_id``. Anything not in this set and
#: not in TENANT_SCOPED_TABLES is a schema mistake.
GLOBAL_TABLES: frozenset[str] = frozenset({"tenant", "app_user", "alembic_version"})

__all__ = [
    "GLOBAL_TABLES",
    "TENANT_SCOPED_TABLES",
    "AppUser",
    "AuditLog",
    "BankAccount",
    "BankTxn",
    "Base",
    "Document",
    "DocumentType",
    "EntityType",
    "ExtractionStatus",
    "Finding",
    "FindingSeverity",
    "FindingStatus",
    "LedgerEntry",
    "LineItem",
    "Link",
    "LinkStatus",
    "LinkType",
    "Membership",
    "MembershipRole",
    "Party",
    "PartyKind",
    "SourceFile",
    "Tenant",
    "TenantMixin",
    "TxnDirection",
]
