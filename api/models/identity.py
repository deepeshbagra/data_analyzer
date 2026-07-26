"""Tenants, users and the membership join.

``app_user`` is intentionally *not* tenant-scoped. A CA-firm user works across
many client tenants, so identity is global and access is expressed as
``membership`` rows. That is what makes the Phase 4 tenant switcher a query
rather than a second auth system.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db.base import Base, TimestampMixin, UUIDPrimaryKey
from api.models.enums import MembershipRole
from api.models.types import pg_enum


class Tenant(Base, UUIDPrimaryKey, TimestampMixin):
    """The isolation boundary. Every other business table points here."""

    __tablename__ = "tenant"

    name: Mapped[str]
    slug: Mapped[str] = mapped_column(unique=True)

    # The tenant's own registration details -- distinct from a `party` row,
    # which describes a counterparty.
    gstin: Mapped[str | None]
    pan: Mapped[str | None]
    country: Mapped[str] = mapped_column(Text, default="IN", server_default="IN")
    base_currency: Mapped[str] = mapped_column(Text, default="INR", server_default="INR")

    # Financial year start month; 4 = April, the Indian default. Drives period
    # close, ageing buckets and the "credit note after period close" rule.
    fy_start_month: Mapped[int] = mapped_column(default=4, server_default="4")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("fy_start_month BETWEEN 1 AND 12", name="fy_start_month_valid"),
        CheckConstraint("char_length(base_currency) = 3", name="base_currency_iso4217"),
    )


class AppUser(Base, UUIDPrimaryKey, TimestampMixin):
    """A human. Global, not tenant-scoped.

    Named ``app_user`` because ``user`` is a reserved word in Postgres.
    """

    __tablename__ = "app_user"

    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    # argon2id. Never logged, never serialised, never returned by an endpoint.
    password_hash: Mapped[str]
    full_name: Mapped[str | None]

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Platform staff. Carries no implicit access to tenant data -- RLS still
    # requires an explicit tenant context.
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_login_at: Mapped[dt.datetime | None]

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Membership(Base, UUIDPrimaryKey, TimestampMixin):
    """Grants one user one role in one tenant.

    Tenant-scoped, but with a wider RLS policy than the other tables: a user
    can always see their own membership rows regardless of the active tenant
    context, because login has to enumerate available tenants before one is
    selected. See migration 0002.
    """

    __tablename__ = "membership"

    # Declared explicitly rather than via TenantMixin so the intent of the
    # broader policy on this table stays visible at the definition site.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[MembershipRole] = mapped_column(pg_enum(MembershipRole))

    # Set for CA-firm memberships so audit trails can distinguish a firm
    # operating on a client from the client's own staff.
    is_external_advisor: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    tenant: Mapped[Tenant] = relationship(back_populates="memberships")
    user: Mapped[AppUser] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_id_user_id"),
        Index("ix_membership_user_id_tenant_id", "user_id", "tenant_id"),
    )


__all__ = ["AppUser", "Membership", "Tenant"]
