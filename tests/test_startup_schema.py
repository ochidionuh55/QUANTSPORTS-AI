"""Automatic schema migration tests.

A missed migration fails in the worst possible way: the service starts, reports
healthy, and every screen touching a new column breaks with nothing shown to
the user. These tests keep the schema the application's own responsibility
rather than a deployment step someone can forget.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.infrastructure.database import Database
from app.infrastructure.migrations import (
    SchemaOutOfDateError,
    ensure_schema,
    head_revision,
    schema_is_current,
)


@pytest.fixture
def settings() -> Settings:
    """Settings pointed at an isolated database."""
    return Settings(_env_file=None)


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    """A connected, empty database."""
    instance = Database(settings)
    await instance.connect()
    yield instance
    await instance.disconnect()


class TestRevisionReading:
    """Knowing where the schema stands."""

    def test_head_revision_is_known(self, settings: Settings) -> None:
        """The codebase must be able to name its newest migration."""
        assert head_revision(settings)

    async def test_empty_database_is_not_current(
        self, settings: Settings, database: Database
    ) -> None:
        assert await schema_is_current(settings, database) is False


class _BrokenDatabase:
    """A database that cannot be read, as an unreachable one behaves."""

    @property
    def engine(self) -> object:
        raise ConnectionError("database is unreachable")


class TestFailureHandling:
    """What happens when the upgrade cannot run."""

    async def test_unreachable_database_is_not_reported_current(self, settings: Settings) -> None:
        """A database we cannot read is never assumed to be up to date."""
        assert await schema_is_current(settings, _BrokenDatabase()) is False  # type: ignore[arg-type]

    async def test_required_failure_raises(self, settings: Settings) -> None:
        """A service that writes must refuse a schema it cannot verify."""
        with pytest.raises(SchemaOutOfDateError):
            await ensure_schema(settings, _BrokenDatabase(), required=True)  # type: ignore[arg-type]

    async def test_optional_failure_returns_false(self, settings: Settings) -> None:
        """The bot reads, so it starts anyway and users get a working
        conversation while the operator is alerted."""
        result = await ensure_schema(
            settings,
            _BrokenDatabase(),
            required=False,  # type: ignore[arg-type]
        )
        assert result is False


class TestErrorMiddleware:
    """A failing handler must answer, not vanish."""

    async def test_failure_is_answered(self) -> None:
        from app.bot.middleware import ErrorMiddleware

        answered: list[str] = []

        class _Callback:
            data = "menu:best"
            message = None

            async def answer(self, text: str = "", show_alert: bool = False) -> None:
                answered.append(text)

        async def failing(event: object, data: dict[str, object]) -> None:
            raise RuntimeError("database is on fire")

        await ErrorMiddleware()(failing, _Callback(), {})  # type: ignore[arg-type]
        assert answered

    async def test_successful_handler_passes_through(self) -> None:
        from app.bot.middleware import ErrorMiddleware

        async def working(event: object, data: dict[str, object]) -> str:
            return "fine"

        result = await ErrorMiddleware()(working, object(), {})  # type: ignore[arg-type]
        assert result == "fine"

    async def test_internal_detail_is_not_shown(self) -> None:
        """A stack trace is information about our systems, not the user's."""
        from app.bot.middleware import ErrorMiddleware

        answered: list[str] = []

        class _Callback:
            data = "menu:best"
            message = None

            async def answer(self, text: str = "", show_alert: bool = False) -> None:
                answered.append(text)

        async def failing(event: object, data: dict[str, object]) -> None:
            raise RuntimeError("relation service_selections does not exist")

        await ErrorMiddleware()(failing, _Callback(), {})  # type: ignore[arg-type]
        assert all("service_selections" not in text for text in answered)
