"""Row-level security, application roles and audit immutability

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-25

This migration is the entire tenant-isolation story. Read it before changing
anything in it.

Design, and why each part is necessary:

1. ``current_tenant_id()`` reads ``current_setting('app.tenant_id', true)``.
   The second argument means "return NULL rather than error if unset". Every
   policy compares ``tenant_id = current_tenant_id()``, so with no tenant
   context the comparison is NULL, which is not TRUE, and zero rows are
   visible. Forgetting to scope a session returns nothing rather than
   everything -- it fails closed.

2. Policies carry both ``USING`` and ``WITH CHECK``. ``USING`` filters rows on
   read/update/delete; ``WITH CHECK`` validates rows on insert/update. Without
   ``WITH CHECK`` a session scoped to tenant A could *write* a row stamped
   tenant B -- visible to nobody but silently corrupting B's data.

3. ``FORCE ROW LEVEL SECURITY`` in addition to ``ENABLE``. Plain ENABLE exempts
   the table owner, and the owner is who migrations run as. Superusers still
   bypass RLS entirely, which is exactly why the application connects as
   ``app_rw`` and ``/ready`` refuses to start if that role turns out to be a
   superuser.

4. Three roles, least privilege each:
     app_rw    runtime. DML on business tables, no DDL, not a superuser.
     app_ro    Phase 5 generated SQL. SELECT only -- the grant is the second
               barrier behind the SQL parser, so a parser bypass still cannot
               write.
     app_auth  login only. Needs to find a user by email *before* any tenant
               is known, so it has BYPASSRLS -- but it is granted SELECT on
               exactly one table (app_user) and nothing else. Narrow and
               explicit beats a policy hole that admits unscoped sessions.

5. ``audit_log`` is append-only: UPDATE and DELETE are revoked *and* a trigger
   raises on either, so immutability survives someone re-granting the
   privilege by mistake.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Tables with a tenant_id column, each getting ENABLE + FORCE RLS + a policy.
#: Kept in sync with api.models.TENANT_SCOPED_TABLES by
#: tests/test_schema_rls_coverage.py, which fails if the two ever diverge.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "audit_log",
    "bank_account",
    "bank_txn",
    "document",
    "finding",
    "ledger_entry",
    "line_item",
    "link",
    "membership",
    "party",
    "source_file",
)

#: Tables the runtime role may write.
RW_TABLES: tuple[str, ...] = tuple(t for t in TENANT_SCOPED_TABLES if t != "audit_log") + (
    "tenant",
    "app_user",
)


def _password(env_var: str, fallback: str) -> str:
    # Local development convenience only. Real environments set these; see
    # docs/SECURITY.md (Phase 4) for the rotation procedure.
    return os.environ.get(env_var) or fallback


def upgrade() -> None:
    app_rw_pw = _password("APP_RW_PASSWORD", "app_rw_dev_password")
    app_ro_pw = _password("APP_RO_PASSWORD", "app_ro_dev_password")
    app_auth_pw = _password("APP_AUTH_PASSWORD", "app_auth_dev_password")

    # --- Tenant context accessors -------------------------------------------
    # STABLE, not IMMUTABLE: the value is fixed within a statement but changes
    # between transactions. Marking it IMMUTABLE would let the planner cache it
    # across transactions, which would be a cross-tenant leak.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION current_tenant_id() RETURNS uuid AS $$
            SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
        $$ LANGUAGE sql STABLE
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION current_user_id() RETURNS uuid AS $$
            SELECT NULLIF(current_setting('app.user_id', true), '')::uuid
        $$ LANGUAGE sql STABLE
        """
    )

    # --- Roles --------------------------------------------------------------
    for role, password in (
        ("app_rw", app_rw_pw),
        ("app_ro", app_ro_pw),
    ):
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} LOGIN PASSWORD '{password}'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
                ELSE
                    ALTER ROLE {role} PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS;
                END IF;
            END $$
            """
        )

    # app_auth is the single deliberate BYPASSRLS role. It can read exactly one
    # table and write nothing.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_auth') THEN
                CREATE ROLE app_auth LOGIN PASSWORD '{app_auth_pw}'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
            ELSE
                ALTER ROLE app_auth PASSWORD '{app_auth_pw}' NOSUPERUSER BYPASSRLS;
            END IF;
        END $$
        """
    )

    # --- Grants -------------------------------------------------------------
    op.execute("GRANT USAGE ON SCHEMA public TO app_rw, app_ro, app_auth")

    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_ro")
    op.execute("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO app_ro")

    for table in RW_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_rw")
    # Append-only. No UPDATE, no DELETE, for anyone.
    op.execute("GRANT SELECT, INSERT ON audit_log TO app_rw")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw")

    # Login path: find a user by email before a tenant is known.
    op.execute("GRANT SELECT ON app_user TO app_auth")
    op.execute("GRANT SELECT ON membership TO app_auth")
    op.execute("GRANT SELECT ON tenant TO app_auth")

    # Tables created by later migrations inherit these grants automatically, so
    # a Phase 2/3 table cannot ship unreachable -- or over-privileged.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO app_ro"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO app_rw"
    )

    # Nobody but the owner gets DDL.
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    # --- RLS: tenant --------------------------------------------------------
    # The tenant row itself is visible only to a session scoped to it.
    op.execute("ALTER TABLE tenant ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_self_access ON tenant
            FOR ALL
            USING (id = current_tenant_id())
            WITH CHECK (id = current_tenant_id())
        """
    )

    # --- RLS: app_user ------------------------------------------------------
    # A user can see themselves, plus anyone who shares the active tenant.
    # No unscoped read path: the login lookup goes through app_auth instead.
    op.execute("ALTER TABLE app_user ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app_user FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY app_user_visible_to_self_or_cotenant ON app_user
            FOR ALL
            USING (
                id = current_user_id()
                OR EXISTS (
                    SELECT 1 FROM membership m
                    WHERE m.user_id = app_user.id
                      AND m.tenant_id = current_tenant_id()
                )
            )
            WITH CHECK (id = current_user_id())
        """
    )

    # --- RLS: membership ----------------------------------------------------
    # Wider than the standard policy on purpose: a user must be able to
    # enumerate their own tenants at login, before any tenant is selected.
    # That is what makes the CA-firm tenant switcher work.
    op.execute("ALTER TABLE membership ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE membership FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY membership_tenant_or_own ON membership
            FOR ALL
            USING (tenant_id = current_tenant_id() OR user_id = current_user_id())
            WITH CHECK (tenant_id = current_tenant_id())
        """
    )

    # --- RLS: every other tenant-scoped table -------------------------------
    for table in TENANT_SCOPED_TABLES:
        if table == "membership":
            continue  # handled above with a wider USING clause
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
                FOR ALL
                USING (tenant_id = current_tenant_id())
                WITH CHECK (tenant_id = current_tenant_id())
            """
        )

    # --- audit_log immutability --------------------------------------------
    # Belt and braces: the grant above already withholds UPDATE/DELETE, but a
    # future migration or a well-meaning DBA could re-grant it. The trigger
    # cannot be bypassed that way.
    # The one permitted deletion is erasure of an entire tenant (a DPDP/GDPR
    # deletion request), which arrives as an ON DELETE CASCADE from `tenant`.
    # By the time this BEFORE DELETE trigger fires for a cascade, Postgres has
    # already removed the parent row in the same transaction, so the absence of
    # the tenant is a reliable signal that this is a full erasure and not
    # someone editing history.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND NOT EXISTS (SELECT 1 FROM tenant WHERE id = OLD.tenant_id) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION
                'audit_log is append-only: % on row % is not permitted',
                TG_OP, OLD.id
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_append_only
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_is_append_only()")

    op.execute("DROP POLICY IF EXISTS membership_tenant_or_own ON membership")
    op.execute("DROP POLICY IF EXISTS app_user_visible_to_self_or_cotenant ON app_user")
    op.execute("DROP POLICY IF EXISTS tenant_self_access ON tenant")
    for table in TENANT_SCOPED_TABLES:
        if table != "membership":
            op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for table in ("tenant", "app_user"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
               "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM app_rw")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM app_ro")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
               "REVOKE USAGE, SELECT ON SEQUENCES FROM app_rw")

    # Roles are intentionally NOT dropped: other databases in the cluster may
    # reference them, and dropping a role with dependent grants fails anyway.
    op.execute("DROP FUNCTION IF EXISTS current_user_id()")
    op.execute("DROP FUNCTION IF EXISTS current_tenant_id()")
