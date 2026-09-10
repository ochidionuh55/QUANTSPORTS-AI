"""Shared test fixtures.

Unit tests run without Postgres or Redis: the infrastructure handles are
replaced with fakes that satisfy the same probe interface. Tests that need real
services must be marked ``@pytest.mark.integration``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Environment, ServiceRole, Settings, get_settings


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove QUANTSPORT-related env vars so tests are hermetic.

    Both nested (``POSTGRES__HOST``) and top-level (``DEBUG``) names must be
    cleared. Clearing only the nested ones made the suite pass on a bare
    machine and fail inside the container, where docker-compose exports the
    whole .env: ``DEBUG=true`` leaked into a test that asserts production
    behaviour.
    """
    nested_prefixes = {
        "POSTGRES",
        "REDIS",
        "TELEGRAM",
        "FEATURES",
        "OBSERVABILITY",
    }
    top_level = {
        "APP_NAME",
        "ENVIRONMENT",
        "SERVICE_ROLE",
        "DEBUG",
        "API_HOST",
        "API_PORT",
        "API_ROOT_PATH",
    }
    for key in list(os.environ):
        if key.split("__")[0] in nested_prefixes or key in top_level:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """Return settings suitable for unit tests."""
    return Settings(
        environment=Environment.TEST,
        service_role=ServiceRole.API,
        _env_file=None,  # type: ignore[call-arg]
    )


class FakeProbe:
    """Minimal stand-in for Database/RedisClient in health tests."""

    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.calls = 0

    async def ping(self, timeout: float | None = None) -> bool:
        """Record the call and return the configured health state."""
        self.calls += 1
        return self.healthy


class FakeRedis(FakeProbe):
    """Fake Redis client with the surface the health report touches."""

    def __init__(self, healthy: bool = True) -> None:
        super().__init__(healthy)
        self.store: dict[str, Any] = {}

    def key(self, *parts: str) -> str:
        """Mirror the real namespacing helper."""
        return ":".join(("quantsport", *parts))

    @property
    def client(self) -> FakeRedis:
        """Return self; the fake is its own client."""
        return self

    async def get(self, key: str) -> Any:
        """Return a stored value or ``None``."""
        return self.store.get(key)

    async def set(self, key: str, value: Any, **_kwargs: Any) -> bool:
        """Store a value, ignoring expiry semantics."""
        self.store[key] = value
        return True

    async def delete(self, key: str) -> int:
        """Delete a key if present."""
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture
def fake_database() -> FakeProbe:
    """Healthy fake database."""
    return FakeProbe(healthy=True)


@pytest.fixture
def fake_redis() -> FakeRedis:
    """Healthy fake Redis client."""
    return FakeRedis(healthy=True)


@pytest.fixture
def app(settings: Settings, fake_database: FakeProbe, fake_redis: FakeRedis) -> FastAPI:
    """Build the API app with infrastructure replaced by fakes.

    The lifespan is bypassed by attaching state directly, so no network
    connections are attempted.
    """
    from app.main import create_app

    application = create_app(settings)
    application.router.lifespan_context = _noop_lifespan  # type: ignore[assignment]
    application.state.database = fake_database
    application.state.redis = fake_redis
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Return a synchronous test client for the API."""
    with TestClient(app) as test_client:
        yield test_client


def _noop_lifespan(_app: FastAPI) -> Any:
    """Lifespan replacement that connects nothing."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx() -> Any:
        yield

    return _ctx()
