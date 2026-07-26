"""Tenant isolation proofs.

Task 3 of the bootstrap: prove that a query from tenant A cannot see tenant B's
rows *even with a raw SQL injection of a different tenant_id*. This file must
keep passing before any feature code ships, and every assertion here runs
through the ``app_rw`` role, never the superuser.

The threat model covered:

  read      a scoped session asking for another tenant's rows by id
  injection a predicate the attacker controls entirely (``OR 1=1``, an explicit
            foreign tenant_id, a UNION reaching a second table)
  write     forging a foreign tenant_id on INSERT, or moving a row to another
            tenant with UPDATE
  unscoped  forgetting to set tenant context at all
  privilege the SELECT-only role attempting to write
  history   anyone attempting to edit or erase the audit trail
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tests.conftest import TwoTenants

pytestmark = pytest.mark.requires_db

# Every tenant-scoped table, checked with the same set of assertions. If a
# later phase adds a table, add it here too -- test_schema_rls_coverage.py
# fails until it exists in both places.
TENANT_TABLES = [
    "party",
    "source_file",
    "document",
    "line_item",
    "bank_account",
    "bank_txn",
    "audit_log",
]


def _scoped(conn: Connection, tenant_id: uuid.UUID, user_id: uuid.UUID | None = None) -> None:
    """Apply tenant context the way ``api.db.session.tenant_session`` does."""
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    conn.execute(
        text("SELECT set_config('app.user_id', :u, true)"),
        {"u": str(user_id) if user_id else None},
    )


# ---------------------------------------------------------------------------
# Preconditions: if these fail, nothing below means anything
# ---------------------------------------------------------------------------


def test_runtime_role_is_not_superuser(rw_engine: Engine) -> None:
    """A superuser bypasses RLS unconditionally, making every policy decorative."""
    with rw_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user AS role, usesuper, usebypassrls "
                "FROM pg_user WHERE usename = current_user"
            )
        ).one()
    assert row.role == "app_rw", f"tests must run as app_rw, got {row.role}"
    assert row.usesuper is False
    assert row.usebypassrls is False


def test_runtime_role_cannot_create_tables(rw_engine: Engine) -> None:
    """No DDL for the application role: schema changes go through migrations."""
    with rw_engine.connect() as conn, pytest.raises(ProgrammingError):
        conn.execute(text("CREATE TABLE should_not_exist (id int)"))


# ---------------------------------------------------------------------------
# Read isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_scoped_session_sees_only_its_own_rows(
    rw_engine: Engine, two_tenants: TwoTenants, table: str
) -> None:
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        foreign = conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id <> :t"),
            {"t": str(two_tenants.a.tenant_id)},
        ).scalar_one()
        own = conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),
            {"t": str(two_tenants.a.tenant_id)},
        ).scalar_one()

    assert foreign == 0, f"{table} leaked rows from another tenant"
    assert own >= 1, f"{table} fixture data missing; the test proves nothing"


def test_explicit_foreign_tenant_id_predicate_returns_nothing(
    rw_engine: Engine, two_tenants: TwoTenants
) -> None:
    """The core injection case: attacker supplies B's tenant_id verbatim.

    This is the query an attacker gets to run if any endpoint interpolates a
    tenant id from a request. RLS ANDs its own predicate on top, so the result
    is empty rather than B's data.
    """
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        rows = conn.execute(
            text(f"SELECT id FROM document WHERE tenant_id = '{two_tenants.b.tenant_id}'")
        ).all()
    assert rows == []


def test_tautology_injection_cannot_widen_the_result(
    rw_engine: Engine, two_tenants: TwoTenants
) -> None:
    """``OR 1=1`` defeats an application-level filter. It does not defeat RLS."""
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        ids = (
            conn.execute(
                text(
                    "SELECT tenant_id FROM document "
                    f"WHERE tenant_id = '{two_tenants.b.tenant_id}' OR 1=1"
                )
            )
            .scalars()
            .all()
        )

    assert ids, "expected tenant A's own documents"
    assert set(ids) == {two_tenants.a.tenant_id}


def test_union_injection_cannot_reach_another_tenant(
    rw_engine: Engine, two_tenants: TwoTenants
) -> None:
    """A UNION into a second table is still filtered on both branches."""
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        ids = (
            conn.execute(
                text(
                    "SELECT tenant_id FROM document "
                    "UNION ALL SELECT tenant_id FROM bank_txn "
                    "UNION ALL SELECT tenant_id FROM party"
                )
            )
            .scalars()
            .all()
        )

    assert set(ids) == {two_tenants.a.tenant_id}


def test_join_across_tenants_yields_no_rows(rw_engine: Engine, two_tenants: TwoTenants) -> None:
    """Even a deliberately cross-tenant join is empty: both sides are filtered."""
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        count = conn.execute(
            text(
                "SELECT count(*) FROM document d JOIN party p ON p.id = d.party_id "
                "WHERE p.tenant_id <> d.tenant_id"
            )
        ).scalar_one()
    assert count == 0


def test_fetch_by_primary_key_of_foreign_row_returns_nothing(
    rw_engine: Engine, two_tenants: TwoTenants
) -> None:
    """Guessing or leaking a UUID is not enough; the row is invisible."""
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        for table, row_id in (
            ("document", two_tenants.b.document_id),
            ("party", two_tenants.b.party_id),
            ("bank_txn", two_tenants.b.bank_txn_id),
            ("source_file", two_tenants.b.source_file_id),
        ):
            found = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE id = :id"),
                {"id": row_id},
            ).scalar_one()
            assert found == 0, f"{table} exposed a foreign row by id"


def test_aggregates_do_not_leak_foreign_amounts(rw_engine: Engine, two_tenants: TwoTenants) -> None:
    """A count or sum is a side channel if policies are missing.

    Both tenants hold an identical 118000.0000 invoice, so an unfiltered sum
    would be exactly double and easy to miss in review. Assert the value, not
    just that it is non-zero.
    """
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        total = conn.execute(
            text("SELECT coalesce(sum(grand_total), 0) FROM document")
        ).scalar_one()
        count = conn.execute(text("SELECT count(*) FROM document")).scalar_one()

    assert count == 1
    assert total == pytest.approx(118000.0)


# ---------------------------------------------------------------------------
# Unscoped sessions fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_session_without_tenant_context_sees_nothing(
    rw_engine: Engine, two_tenants: TwoTenants, table: str
) -> None:
    """No ``app.tenant_id`` at all must mean no rows, not all rows.

    ``current_tenant_id()`` returns NULL, ``tenant_id = NULL`` is NULL, and NULL
    is not TRUE -- so the policy rejects every row. Forgetting to scope a
    session is a visible bug, never a silent breach.
    """
    with rw_engine.begin() as conn:
        count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    assert count == 0


def test_tenant_context_does_not_survive_the_transaction(
    rw_engine: Engine, two_tenants: TwoTenants
) -> None:
    """``set_config(..., true)`` is transaction-local.

    This is what makes connection pooling safe: the next request to reuse this
    connection starts with no tenant context rather than inheriting the
    previous request's.
    """
    with rw_engine.connect() as conn:
        with conn.begin():
            _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
            assert conn.execute(text("SELECT count(*) FROM document")).scalar_one() == 1

        with conn.begin():
            leaked = conn.execute(text("SELECT current_tenant_id()")).scalar_one()
            assert leaked is None
            assert conn.execute(text("SELECT count(*) FROM document")).scalar_one() == 0


def test_empty_tenant_setting_is_treated_as_unset(rw_engine: Engine) -> None:
    """An empty string must not become a cast error or a wildcard."""
    with rw_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        assert conn.execute(text("SELECT current_tenant_id()")).scalar_one() is None
        assert conn.execute(text("SELECT count(*) FROM document")).scalar_one() == 0


# ---------------------------------------------------------------------------
# Write isolation
# ---------------------------------------------------------------------------


def test_cannot_insert_a_row_stamped_with_a_foreign_tenant(
    rw_engine: Engine, two_tenants: TwoTenants
) -> None:
    """This is what the ``WITH CHECK`` half of each policy exists for.

    ``USING`` alone would permit the write and merely hide the result --
    corrupting tenant B's data with a row nobody can see or explain.
    """
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        with pytest.raises(DBAPIError) as exc:
            conn.execute(
                text(
                    "INSERT INTO party (tenant_id, kind, legal_name, display_name) "
                    f"VALUES ('{two_tenants.b.tenant_id}', 'vendor', 'Injected', 'Injected')"
                )
            )
    assert "row-level security" in str(exc.value).lower()


def test_cannot_move_a_row_to_another_tenant(rw_engine: Engine, two_tenants: TwoTenants) -> None:
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        with pytest.raises(DBAPIError) as exc:
            conn.execute(
                text(
                    "UPDATE document SET tenant_id = "
                    f"'{two_tenants.b.tenant_id}' WHERE id = '{two_tenants.a.document_id}'"
                )
            )
    assert "row-level security" in str(exc.value).lower()


def test_update_cannot_touch_a_foreign_row(
    rw_engine: Engine, two_tenants: TwoTenants, admin_engine: Engine
) -> None:
    """A silent zero-row UPDATE, not an error -- and B's data is unchanged."""
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        result = conn.execute(
            text("UPDATE document SET number = 'TAMPERED' WHERE id = :id"),
            {"id": two_tenants.b.document_id},
        )
        assert result.rowcount == 0

    with admin_engine.connect() as conn:
        number = conn.execute(
            text("SELECT number FROM document WHERE id = :id"),
            {"id": two_tenants.b.document_id},
        ).scalar_one()
    assert number == two_tenants.b.invoice_number


def test_delete_cannot_touch_a_foreign_row(
    rw_engine: Engine, two_tenants: TwoTenants, admin_engine: Engine
) -> None:
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        result = conn.execute(
            text("DELETE FROM party WHERE id = :id"), {"id": two_tenants.b.party_id}
        )
        assert result.rowcount == 0

    with admin_engine.connect() as conn:
        alive = conn.execute(
            text("SELECT count(*) FROM party WHERE id = :id"),
            {"id": two_tenants.b.party_id},
        ).scalar_one()
    assert alive == 1


# ---------------------------------------------------------------------------
# Membership: the one deliberately wider policy
# ---------------------------------------------------------------------------


def test_user_can_enumerate_own_memberships_without_tenant_context(
    rw_engine: Engine, two_tenants: TwoTenants
) -> None:
    """Login needs this: pick a tenant before you have one.

    The policy widens on ``user_id``, not on absence of context, so this path
    still cannot see anyone else's memberships.
    """
    with rw_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.user_id', :u, true)"),
            {"u": str(two_tenants.a.user_id)},
        )
        rows = conn.execute(text("SELECT tenant_id, user_id FROM membership")).all()

    assert len(rows) == 1
    assert rows[0].tenant_id == two_tenants.a.tenant_id
    assert rows[0].user_id == two_tenants.a.user_id


def test_membership_widening_does_not_expose_other_users(
    rw_engine: Engine, two_tenants: TwoTenants
) -> None:
    with rw_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.user_id', :u, true)"),
            {"u": str(two_tenants.a.user_id)},
        )
        count = conn.execute(
            text("SELECT count(*) FROM membership WHERE user_id = :u"),
            {"u": two_tenants.b.user_id},
        ).scalar_one()
    assert count == 0


def test_cotenant_users_are_visible_but_others_are_not(
    rw_engine: Engine, two_tenants: TwoTenants
) -> None:
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        visible = conn.execute(text("SELECT id FROM app_user")).scalars().all()

    assert two_tenants.a.user_id in visible
    assert two_tenants.b.user_id not in visible


def test_tenant_row_itself_is_scoped(rw_engine: Engine, two_tenants: TwoTenants) -> None:
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        ids = conn.execute(text("SELECT id FROM tenant")).scalars().all()
    assert ids == [two_tenants.a.tenant_id]


# ---------------------------------------------------------------------------
# Privilege separation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO party (tenant_id, kind, legal_name, display_name) "
        "VALUES (current_tenant_id(), 'vendor', 'x', 'x')",
        "UPDATE document SET number = 'x'",
        "DELETE FROM document",
        "CREATE TABLE nope (id int)",
        "DROP TABLE document",
    ],
)
def test_readonly_role_cannot_write(
    ro_engine: Engine, two_tenants: TwoTenants, statement: str
) -> None:
    """Phase 5 runs model-generated SQL on this role.

    The validating parser is the first barrier; this grant is the second. A
    parser bypass must still be unable to change anything.
    """
    with ro_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        with pytest.raises(ProgrammingError):
            conn.execute(text(statement))


def test_readonly_role_is_still_tenant_scoped(ro_engine: Engine, two_tenants: TwoTenants) -> None:
    """SELECT-only is not a licence to read across tenants."""
    with ro_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        ids = conn.execute(text("SELECT tenant_id FROM document")).scalars().all()
    assert set(ids) == {two_tenants.a.tenant_id}


# ---------------------------------------------------------------------------
# Audit trail immutability
# ---------------------------------------------------------------------------


def test_audit_log_cannot_be_updated(rw_engine: Engine, two_tenants: TwoTenants) -> None:
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        with pytest.raises(ProgrammingError):
            conn.execute(text("UPDATE audit_log SET action = 'rewritten'"))


def test_audit_log_cannot_be_deleted(rw_engine: Engine, two_tenants: TwoTenants) -> None:
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        with pytest.raises(ProgrammingError):
            conn.execute(text("DELETE FROM audit_log"))


def test_audit_log_append_is_allowed(rw_engine: Engine, two_tenants: TwoTenants) -> None:
    """Append-only means append must actually work."""
    with rw_engine.begin() as conn:
        _scoped(conn, two_tenants.a.tenant_id, two_tenants.a.user_id)
        conn.execute(
            text(
                "INSERT INTO audit_log (tenant_id, actor, action, entity_type, entity_id) "
                "VALUES (current_tenant_id(), current_user_id(), 'document.viewed', "
                "'document', :d)"
            ),
            {"d": two_tenants.a.document_id},
        )
        count = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'document.viewed'")
        ).scalar_one()
    assert count == 1
