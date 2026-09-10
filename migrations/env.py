"""Alembic migration environment.

The database URL comes from application settings rather than ``alembic.ini``,
so migrations and the running application can never point at different
databases, and no credentials are committed.

Importing ``app.database.models`` is what populates ``Base.metadata``; without
it autogenerate would see an empty schema and helpfully offer to drop every
table.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.database import models  # noqa: F401 - registers every model
from app.database.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """Return the database URL from application settings.

    ``ALEMBIC_URL`` overrides it, which is useful for inspecting SQL against a
    throwaway database. Never use the override to generate a migration that
    will run on PostgreSQL: dialect-specific types such as JSONB render as
    their portable fallback under another dialect, producing a migration that
    silently creates the wrong column types.
    """
    override = os.getenv("ALEMBIC_URL")
    return override or get_settings().postgres.dsn


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render custom column types faithfully in generated migrations.

    Alembic's default renderer discards two things we rely on:

    * ``native_enum=False`` on Enum columns. Without this, autogenerate emits a
      native PostgreSQL enum type that was never created, and the migration
      fails on the first table.
    * The JSONB variant on JSON columns. Without this, PostgreSQL gets plain
      JSON, losing indexing and containment operators.

    Returning ``False`` falls back to the default renderer.
    """
    if type_ != "type":
        return False

    if isinstance(obj, sa.Enum):
        values = ", ".join(repr(value) for value in obj.enums)
        return (
            f"sa.Enum({values}, name={obj.name!r}, " f"native_enum=False, create_constraint=True)"
        )

    if isinstance(obj, sa.JSON):
        autogen_context.imports.add(  # type: ignore[attr-defined]
            "from sqlalchemy.dialects import postgresql"
        )
        return 'sa.JSON().with_variant(postgresql.JSONB(), "postgresql")'

    return False


def _configure(connection: Connection) -> None:
    """Apply shared configuration to a migration context."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
        # SQLite cannot ALTER COLUMN, so column changes must be applied by
        # rebuilding the table. Enabling batch mode only for SQLite keeps
        # migrations verifiable against a throwaway database without changing
        # how they run on PostgreSQL.
        render_as_batch=connection.dialect.name == "sqlite",
        include_schemas=False,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an established connection."""
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Connect asynchronously and run migrations."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
