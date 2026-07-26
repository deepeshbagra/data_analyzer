"""Helpers for mapping Python enums onto pre-created Postgres enum types."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SAEnum

from api.models.enums import PG_ENUM_NAMES


def pg_enum(enum_cls: type[StrEnum]) -> SAEnum:
    """Bind a ``StrEnum`` to its native Postgres type.

    ``values_callable`` makes SQLAlchemy persist the member *values*
    (``"purchase_invoice"``) rather than the member *names*
    (``"PURCHASE_INVOICE"``), so what is in the database is what appears in
    JSON payloads and hand-written SQL.

    ``create_type=False`` because the types are created explicitly in migration
    0001; letting the ORM create them implicitly makes migration ordering
    unpredictable.
    """
    return SAEnum(
        enum_cls,
        name=PG_ENUM_NAMES[enum_cls],
        native_enum=True,
        create_type=False,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )
