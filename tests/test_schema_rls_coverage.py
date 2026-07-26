"""Structural guarantees about the schema itself.

``test_tenant_isolation.py`` proves the policies work on the tables it names.
This file proves there are no tables it forgot to name. It reads the live
Postgres catalog and the SQLAlchemy metadata and cross-checks them, so a table
added in Phase 2, 3 or 4 without a policy fails here immediately rather than
shipping unprotected.

It also enforces the money rule structurally: every amount column is
numeric(18,4), and no column anywhere is a float type.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from api.models import GLOBAL_TABLES, TENANT_SCOPED_TABLES
from migrations.versions import rls_table_list

pytestmark = pytest.mark.requires_db

#: Tables without a tenant_id that still need RLS, because they are reachable
#: by identity rather than by tenant.
GLOBAL_TABLES_WITH_RLS = ("tenant", "app_user")


def test_model_metadata_and_migration_agree_on_scoped_tables() -> None:
    """The migration hardcodes a table list; the models derive one.

    If these drift, some table has a tenant_id but no policy -- or a policy
    naming a table that no longer exists.
    """
    assert set(TENANT_SCOPED_TABLES) == set(rls_table_list())


@pytest.mark.parametrize("table", TENANT_SCOPED_TABLES + GLOBAL_TABLES_WITH_RLS)
def test_rls_is_enabled_and_forced(admin_engine: Engine, table: str) -> None:
    """ENABLE alone exempts the table owner, which is who migrations run as.

    FORCE is what makes the policy bind to everyone short of a superuser.
    """
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                # CAST(:t AS regclass), not :t::regclass -- SQLAlchemy's bind
                # param regex ends with a negative lookahead that rejects a
                # following ':', so the '::' cast makes it skip :t entirely and
                # ship the literal text to Postgres. Applies to all three casts
                # in this file.
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE oid = CAST(:t AS regclass)"
            ),
            {"t": table},
        ).one()
    assert row.relrowsecurity is True, f"{table} does not have RLS enabled"
    assert row.relforcerowsecurity is True, f"{table} does not have RLS forced"


@pytest.mark.parametrize("table", TENANT_SCOPED_TABLES + GLOBAL_TABLES_WITH_RLS)
def test_every_protected_table_has_a_policy(admin_engine: Engine, table: str) -> None:
    """RLS with no policy denies everything, which is safe but breaks the app.

    RLS with a policy that omits WITH CHECK permits forged writes. Assert both
    halves are present.
    """
    with admin_engine.connect() as conn:
        policies = conn.execute(
            text(
                "SELECT polname, polqual IS NOT NULL AS has_using, "
                "       polwithcheck IS NOT NULL AS has_check "
                "FROM pg_policy WHERE polrelid = CAST(:t AS regclass)"
            ),
            {"t": table},
        ).all()

    assert policies, f"{table} has RLS enabled but no policy at all"
    assert any(p.has_using for p in policies), f"{table} has no USING clause"
    assert any(p.has_check for p in policies), (
        f"{table} has no WITH CHECK clause; a scoped session could forge a "
        f"row belonging to another tenant"
    )


def test_no_table_is_left_unprotected(admin_engine: Engine) -> None:
    """Catch the case nobody thought to parametrize: a brand new table."""
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r'"
            )
        ).all()

    unprotected = {
        r.relname for r in rows if not r.relrowsecurity and r.relname not in GLOBAL_TABLES
    }
    assert not unprotected, (
        f"tables without row-level security: {sorted(unprotected)}. Either add a "
        f"policy in a migration or, if the table is genuinely global, add it to "
        f"api.models.GLOBAL_TABLES with a comment explaining why."
    )


@pytest.mark.parametrize("table", TENANT_SCOPED_TABLES)
def test_tenant_id_is_not_nullable_and_indexed(admin_engine: Engine, table: str) -> None:
    """A nullable tenant_id is an orphan row waiting to happen.

    The index matters too: the policy predicate runs on every query, so it has
    to be index-backed rather than turning each read into a scan.
    """
    with admin_engine.connect() as conn:
        nullable = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'tenant_id'"
            ),
            {"t": table},
        ).scalar_one()
        indexed = conn.execute(
            text(
                "SELECT count(*) FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "                   AND a.attnum = i.indkey[0] "
                "WHERE i.indrelid = CAST(:t AS regclass) AND a.attname = 'tenant_id'"
            ),
            {"t": table},
        ).scalar_one()

    assert nullable == "NO", f"{table}.tenant_id is nullable"
    assert indexed >= 1, f"{table} has no index leading with tenant_id"


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

MONEY_COLUMNS = {
    ("document", "subtotal"),
    ("document", "tax_total"),
    ("document", "grand_total"),
    ("line_item", "qty"),
    ("line_item", "rate"),
    ("line_item", "amount"),
    ("ledger_entry", "debit"),
    ("ledger_entry", "credit"),
    ("bank_txn", "amount"),
    ("bank_txn", "balance"),
    ("link", "matched_amount"),
}


def test_no_floating_point_columns_anywhere(admin_engine: Engine) -> None:
    """Principle: never float for money.

    Enforced across the whole schema rather than per column, because the failure
    mode -- a few paise lost per row, compounding over a reconciliation -- is
    invisible in review and very visible to an accountant.
    """
    with admin_engine.connect() as conn:
        offenders = conn.execute(
            text(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "  AND data_type IN ('real', 'double precision')"
            )
        ).all()

    assert not offenders, f"floating point columns found: {offenders}"


@pytest.mark.parametrize(("table", "column"), sorted(MONEY_COLUMNS))
def test_money_columns_are_numeric_18_4(admin_engine: Engine, table: str, column: str) -> None:
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).one()
    assert row.data_type == "numeric"
    assert (row.numeric_precision, row.numeric_scale) == (18, 4)


# ---------------------------------------------------------------------------
# Deliberate absences
# ---------------------------------------------------------------------------


def test_document_number_is_not_unique_per_party(admin_engine: Engine) -> None:
    """This absence is load-bearing, so it gets a test.

    The findings engine has rules for duplicate invoices and repeated invoice
    numbers. If someone "tightens" the schema with a unique constraint here,
    those rules can never fire and the fraud they detect becomes invisible.
    """
    with admin_engine.connect() as conn:
        uniques = (
            conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'document' AND indexdef LIKE '%UNIQUE%'"
                )
            )
            .scalars()
            .all()
        )

    for definition in uniques:
        assert "number" not in definition, (
            "document.number must not be part of a unique index: duplicate and "
            "repeated invoice numbers have to be storable in order to be detected"
        )
