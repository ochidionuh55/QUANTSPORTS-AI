"""Health endpoint tests.

The critical assertion is that liveness stays 200 while readiness goes 503:
conflating them causes orchestrators to restart healthy processes during a
database blip, turning a brief outage into a crash loop.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.conftest import FakeProbe, FakeRedis


class TestLiveness:
    """Liveness must never depend on external services."""

    def test_returns_up(self, client: TestClient) -> None:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "up"

    def test_stays_up_when_dependencies_are_down(
        self, client: TestClient, fake_database: FakeProbe, fake_redis: FakeRedis
    ) -> None:
        fake_database.healthy = False
        fake_redis.healthy = False
        assert client.get("/health/live").status_code == 200

    def test_does_not_touch_dependencies(
        self, client: TestClient, fake_database: FakeProbe
    ) -> None:
        client.get("/health/live")
        assert fake_database.calls == 0


class TestReadiness:
    """Readiness reflects whether required dependencies are reachable."""

    def test_ready_when_all_up(self, client: TestClient) -> None:
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "up"
        assert {c["name"] for c in body["components"]} == {"postgres", "redis"}

    def test_not_ready_when_database_down(
        self, client: TestClient, fake_database: FakeProbe
    ) -> None:
        fake_database.healthy = False
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "down"

    def test_not_ready_when_redis_down(self, client: TestClient, fake_redis: FakeRedis) -> None:
        fake_redis.healthy = False
        assert client.get("/health/ready").status_code == 503

    def test_excludes_sibling_processes(self, client: TestClient) -> None:
        names = {c["name"] for c in client.get("/health/ready").json()["components"]}
        assert "worker-process" not in names


class TestDetailedHealth:
    """The detailed report includes siblings but does not fail on them."""

    def test_degraded_when_siblings_missing(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        names = {c["name"] for c in body["components"]}
        assert {"bot-process", "worker-process"} <= names

    def test_up_when_siblings_have_heartbeats(
        self, client: TestClient, fake_redis: FakeRedis
    ) -> None:
        for role in ("bot", "worker"):
            fake_redis.store[f"quantsport:heartbeat:{role}"] = json.dumps({"service": role})
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "up"

    def test_reports_latency(self, client: TestClient) -> None:
        components = client.get("/health").json()["components"]
        postgres = next(c for c in components if c["name"] == "postgres")
        assert postgres["latency_ms"] is not None


class TestCorrelationId:
    """Every response carries a correlation ID for cross-service tracing."""

    def test_generated_when_absent(self, client: TestClient) -> None:
        response = client.get("/health/live")
        assert response.headers["X-Correlation-ID"]

    def test_inbound_id_is_preserved(self, client: TestClient) -> None:
        response = client.get("/health/live", headers={"X-Correlation-ID": "trace-abc-123"})
        assert response.headers["X-Correlation-ID"] == "trace-abc-123"


class TestSystemInfo:
    """Feature-flag state must be observable and secret-free."""

    def test_reports_flags(self, client: TestClient) -> None:
        body = client.get("/system/info").json()
        assert body["features"]["value_detection_enabled"] is False
        assert body["features"]["research_mode"] is True

    def test_contains_no_credentials(self, client: TestClient) -> None:
        raw = client.get("/system/info").text
        assert "password" not in raw.lower()
        assert "postgresql" not in raw.lower()
