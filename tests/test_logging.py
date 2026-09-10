"""Structured logging tests."""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.core.logging import (
    clear_correlation_id,
    configure_logging,
    get_correlation_id,
    get_logger,
    set_correlation_id,
)


@pytest.fixture(autouse=True)
def _reset_correlation_id() -> None:
    """Ensure no correlation ID leaks between tests."""
    clear_correlation_id()


class TestCorrelationContext:
    """Correlation IDs are bound per context and cleared explicitly."""

    def test_generates_when_none_supplied(self) -> None:
        value = set_correlation_id(None)
        assert value
        assert get_correlation_id() == value

    def test_preserves_supplied_value(self) -> None:
        set_correlation_id("abc-123")
        assert get_correlation_id() == "abc-123"

    def test_clear_removes_value(self) -> None:
        set_correlation_id("abc-123")
        clear_correlation_id()
        assert get_correlation_id() is None


class TestJsonOutput:
    """Production logs must be machine-parseable and context-rich."""

    def test_emits_valid_json_with_context(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings.observability.log_format = "json"
        configure_logging(settings)
        set_correlation_id("trace-xyz")

        get_logger("test").info("scan.started", matches=42)

        line = capsys.readouterr().out.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["event"] == "scan.started"
        assert payload["matches"] == 42
        assert payload["correlation_id"] == "trace-xyz"
        assert payload["service"] == "api"
        assert payload["level"] == "info"
        assert "timestamp" in payload

    def test_omits_correlation_id_when_unset(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings.observability.log_format = "json"
        configure_logging(settings)

        get_logger("test").info("worker.idle")

        line = capsys.readouterr().out.strip().splitlines()[-1]
        assert "correlation_id" not in json.loads(line)

    def test_respects_log_level(
        self, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings.observability.log_level = "WARNING"
        settings.observability.log_format = "json"
        configure_logging(settings)

        logger = get_logger("test")
        logger.debug("should.not.appear")
        logger.warning("should.appear")

        output = capsys.readouterr().out
        assert "should.not.appear" not in output
        assert "should.appear" in output
