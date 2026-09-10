"""Declarative base and shared mixins.

A deterministic naming convention is set here so that Alembic generates stable
constraint names. Without it, autogenerate produces anonymous names that differ
between environments and make downgrades unreliable.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, TypeVar

from sqlalchemy import JSON, BigInteger, DateTime, Integer, MetaData, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


JSONType: TypeEngine[Any] = JSON().with_variant(JSONB, "postgresql")
"""Portable JSON column: JSONB on PostgreSQL, plain JSON elsewhere.

Using the variant rather than JSONB directly lets the test suite build the
full schema against in-memory SQLite, which keeps model and constraint tests
fast and free of a database dependency.
"""


_E = TypeVar("_E", bound=StrEnum)


def enum_column(enum_class: type[_E], name: str) -> SAEnum:
    """Build a checked VARCHAR column for a ``StrEnum``.

    Deliberately NOT a native PostgreSQL enum. Adding a value to a native enum
    requires ALTER TYPE, which cannot run inside a transaction and therefore
    cannot be rolled back cleanly. Given that statuses here will certainly gain
    values in later phases, a VARCHAR with a CHECK constraint is the cheaper
    long-term choice and is portable to SQLite for testing.

    Args:
        enum_class: The enum to store.
        name: Constraint name, unique within the schema.
    """
    return SAEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        """Return a concise, secret-free representation."""
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


class IntPrimaryKeyMixin:
    """64-bit surrogate primary key.

    BigInteger throughout: ``odds_snapshots`` and ``outcomes`` will exhaust a
    32-bit key at production volumes, and a mixed key-width schema is worse
    than a uniformly wide one.
    """

    id: Mapped[int] = mapped_column(
        # SQLite only autoincrements a column declared exactly INTEGER, so the
        # test dialect gets Integer while PostgreSQL keeps BIGINT.
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


class TimestampMixin:
    """Creation and update timestamps, both timezone-aware.

    Defaults are applied by the database rather than Python so that rows
    inserted by migrations, fixtures or manual SQL are also stamped.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CreatedAtMixin:
    """Creation timestamp only, for append-only and immutable tables."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )


def json_default() -> dict[str, Any]:
    """Return an empty JSON object, for use as a column default factory."""
    return {}
