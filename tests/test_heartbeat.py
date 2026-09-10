"""Heartbeat and distributed-lock tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.config import ServiceRole, Settings
from app.infrastructure.heartbeat import (
    heartbeat_key,
    read_heartbeat,
    write_heartbeat,
)
from app.worker.main import with_lock
from tests.conftest import FakeRedis


class TestHeartbeatRecords:
    """Heartbeats must be readable, typed and TTL-bounded."""

    async def test_write_then_read(self, settings: Settings, fake_redis: FakeRedis) -> None:
        await write_heartbeat(fake_redis, ServiceRole.WORKER, settings)  # type: ignore[arg-type]
        record = await read_heartbeat(fake_redis, ServiceRole.WORKER)  # type: ignore[arg-type]
        assert record is not None
        assert record["service"] == "worker"
        assert "timestamp" in record

    async def test_missing_heartbeat_returns_none(self, fake_redis: FakeRedis) -> None:
        assert await read_heartbeat(fake_redis, ServiceRole.BOT) is None  # type: ignore[arg-type]

    async def test_malformed_heartbeat_returns_none(self, fake_redis: FakeRedis) -> None:
        fake_redis.store[heartbeat_key(fake_redis, ServiceRole.BOT)] = "not-json"  # type: ignore[arg-type]
        assert await read_heartbeat(fake_redis, ServiceRole.BOT) is None  # type: ignore[arg-type]

    async def test_ttl_is_applied(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        class RecordingRedis(FakeRedis):
            async def set(self, key: str, value: Any, **kwargs: Any) -> bool:
                captured.update({"key": key, "value": value, **kwargs})
                return True

        redis = RecordingRedis()
        await write_heartbeat(redis, ServiceRole.BOT, settings)  # type: ignore[arg-type]
        assert captured["ex"] == settings.observability.heartbeat_ttl_seconds
        assert json.loads(captured["value"])["service"] == "bot"

    def test_keys_are_namespaced(self, fake_redis: FakeRedis) -> None:
        key = heartbeat_key(fake_redis, ServiceRole.WORKER)  # type: ignore[arg-type]
        assert key == "quantsport:heartbeat:worker"


class TestDistributedLock:
    """Scheduled jobs must not double-execute across worker replicas."""

    async def test_job_runs_when_lock_is_free(self, fake_redis: FakeRedis) -> None:
        calls = []

        async def job() -> None:
            calls.append(1)

        await with_lock(fake_redis, "test-job", job)  # type: ignore[arg-type]
        assert calls == [1]

    async def test_job_skipped_when_lock_held(self, fake_redis: FakeRedis) -> None:
        calls = []

        async def job() -> None:
            calls.append(1)

        class ContendedRedis(FakeRedis):
            async def set(self, key: str, value: Any, **kwargs: Any) -> bool | None:
                return None if kwargs.get("nx") else True

        await with_lock(ContendedRedis(), "test-job", job)  # type: ignore[arg-type]
        assert calls == []

    async def test_lock_released_after_failure(self, fake_redis: FakeRedis) -> None:
        async def failing_job() -> None:
            raise RuntimeError("job blew up")

        with pytest.raises(RuntimeError):
            await with_lock(fake_redis, "test-job", failing_job)  # type: ignore[arg-type]

        assert "quantsport:lock:test-job" not in fake_redis.store
