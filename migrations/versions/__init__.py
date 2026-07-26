"""Revision modules.

Alembic skips files beginning with an underscore when scanning for revisions,
so this package marker is invisible to it.

Revision module names begin with a digit and are therefore not valid Python
identifiers -- they can only be imported via importlib, which is what the
accessor below is for.
"""

from __future__ import annotations

import importlib
from typing import cast


def rls_table_list() -> tuple[str, ...]:
    """The tenant-scoped table list that migration 0002 attaches policies to.

    Exposed so ``tests/test_schema_rls_coverage.py`` can assert it matches the
    tables the models actually define. Drift between the two means a table has a
    ``tenant_id`` but no policy protecting it.
    """
    module = importlib.import_module("migrations.versions.0002_rls_and_roles")
    return cast(tuple[str, ...], module.TENANT_SCOPED_TABLES)
