"""Schema invariant tests.

These guard properties that are easy to break silently in a later phase and
expensive to discover in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Enum as SAEnum

from app.database.base import Base
from app.database.models import *  # noqa: F403 - registers every model


def _enum_columns() -> list[tuple[str, str, SAEnum]]:
    """Return every enum-typed column in the schema."""
    found = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        for column in table.columns:
            if isinstance(column.type, SAEnum):
                found.append((table_name, column.name, column.type))
    return found


class TestEnumColumns:
    """Enum storage must be uniform across every table."""

    def test_schema_has_enum_columns(self) -> None:
        assert _enum_columns(), "expected enum columns to exist"

    @pytest.mark.parametrize(
        ("table", "column", "enum_type"),
        _enum_columns(),
        ids=[f"{t}.{c}" for t, c, _ in _enum_columns()],
    )
    def test_stores_values_not_member_names(
        self, table: str, column: str, enum_type: SAEnum
    ) -> None:
        """Every enum column must persist enum *values*, not member names.

        Regression: ``predictions.margin_method`` was declared without
        ``values_callable`` and so persisted "PROPORTIONAL" while
        ``model_versions.margin_method`` persisted "proportional". The same
        Python enum round-tripped as two different strings depending on the
        table, so a query filtering on a literal value matched one and silently
        returned nothing from the other.
        """
        assert all(value == value.lower() for value in enum_type.enums), (
            f"{table}.{column} stores member names {enum_type.enums}; "
            "declare it with enum_column() so it stores lowercase values."
        )

    @pytest.mark.parametrize(
        ("table", "column", "enum_type"),
        _enum_columns(),
        ids=[f"{t}.{c}" for t, c, _ in _enum_columns()],
    )
    def test_is_not_a_native_enum(self, table: str, column: str, enum_type: SAEnum) -> None:
        """Native PostgreSQL enums need ALTER TYPE, which cannot be rolled back."""
        assert (
            enum_type.native_enum is False
        ), f"{table}.{column} is a native enum; use enum_column() instead."


class TestForeignKeyIndexes:
    """PostgreSQL never indexes the referencing side of a foreign key."""

    def test_every_foreign_key_is_covered(self) -> None:
        uncovered: list[str] = []
        for table_name, table in sorted(Base.metadata.tables.items()):
            leading = {
                next(iter(index.columns)).name for index in table.indexes if len(index.columns) > 0
            }
            leading |= {
                next(iter(constraint.columns)).name
                for constraint in table.constraints
                if constraint.__class__.__name__ == "UniqueConstraint"
                and len(constraint.columns) > 0
            }
            for column in table.columns:
                if column.foreign_keys and not column.primary_key and column.name not in leading:
                    uncovered.append(f"{table_name}.{column.name}")

        assert uncovered == [], (
            "Foreign keys without a covering index force a full scan of the "
            f"child table on parent delete: {uncovered}"
        )


class TestIdentifierWidths:
    """A fixture identifier must fit everywhere it is copied.

    Regression: ``historical_matches.provider_match_id`` allowed 128 characters
    while the settlement and analysis tables allowed 64. The mismatch was
    invisible until a Japanese fixture — "Sanfrecce Hiroshima" against
    "Hokkaido Consadole Sapporo" — produced a 61-character identifier and the
    backfill aborted on a competition the ingest had happily accepted.
    """

    def test_downstream_columns_are_no_narrower_than_the_source(self) -> None:
        tables = Base.metadata.tables
        source = tables["historical_matches"].c["provider_match_id"].type.length

        for table_name, column_name in (
            ("settled_predictions", "provider_event_id"),
            ("stored_analyses", "provider_event_id"),
        ):
            width = tables[table_name].c[column_name].type.length
            assert width >= source, (
                f"{table_name}.{column_name} holds {width} characters but "
                f"identifiers are built to {source}. A value accepted by "
                "ingestion would be rejected downstream."
            )

    def test_width_accommodates_the_longest_realistic_identifier(self) -> None:
        """Built from competition, date and both club names."""
        longest = "JAP-2019-05-12-Sanfrecce Hiroshima-Hokkaido Consadole Sapporo"
        width = Base.metadata.tables["settled_predictions"].c["provider_event_id"].type.length
        assert width >= len(longest)

    def test_team_names_fit_alongside_their_identifiers(self) -> None:
        """A club name must not be truncated where the identifier is not."""
        tables = Base.metadata.tables
        for table_name in ("settled_predictions", "stored_analyses"):
            for column_name in ("home_name", "away_name"):
                assert tables[table_name].c[column_name].type.length >= 128


class TestPopulatedTableSafety:
    """A NOT NULL column must be addable to a table that already has rows.

    Regression: ``stored_analyses.provenance`` was added NOT NULL with no
    server default. The migration succeeded on an empty database and failed on
    every real one, because Postgres had nothing to put in the existing rows.
    """

    def test_non_nullable_json_columns_have_a_server_default(self) -> None:
        from sqlalchemy import JSON

        offenders: list[str] = []
        for table_name, table in sorted(Base.metadata.tables.items()):
            for column in table.columns:
                if column.primary_key or column.nullable:
                    continue
                if not isinstance(column.type, JSON):
                    continue
                if column.server_default is None:
                    offenders.append(f"{table_name}.{column.name}")

        assert offenders == [], (
            "These columns cannot be added to a populated table: "
            f"{offenders}. Give each a server_default."
        )

    def test_non_nullable_boolean_columns_have_a_server_default(self) -> None:
        """Same trap, different type."""
        from sqlalchemy import Boolean

        offenders = [
            f"{name}.{column.name}"
            for name, table in sorted(Base.metadata.tables.items())
            for column in table.columns
            if isinstance(column.type, Boolean)
            and not column.nullable
            and column.server_default is None
        ]
        assert offenders == [], f"Boolean columns needing a server_default: {offenders}"
