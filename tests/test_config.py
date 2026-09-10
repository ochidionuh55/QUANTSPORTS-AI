"""Configuration tests.

The feature-flag tests are the important ones: they assert that the governance
decisions from the architecture review cannot be bypassed by configuration.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import (
    Environment,
    FeatureFlags,
    ObservabilitySettings,
    PostgresSettings,
    RedisSettings,
    ServiceRole,
    Settings,
)


class TestFeatureFlagGate:
    """The backtest gate must be enforced in code, not documentation."""

    def test_defaults_are_research_only(self) -> None:
        flags = FeatureFlags(_env_file=None)  # type: ignore[call-arg]
        assert flags.research_mode is True
        assert flags.value_detection_enabled is False
        assert flags.booking_codes_enabled is False
        assert flags.payments_enabled is False

    def test_value_detection_requires_promoted_model(self) -> None:
        with pytest.raises(ValidationError, match="PROMOTED_MODEL_VERSION"):
            FeatureFlags(value_detection_enabled=True, _env_file=None)  # type: ignore[call-arg]

    def test_value_detection_allowed_with_promoted_model(self) -> None:
        flags = FeatureFlags(
            promoted_model_version="ensemble-v1",
            value_detection_enabled=True,
            _env_file=None,  # type: ignore[call-arg]
        )
        assert flags.value_detection_enabled is True

    def test_booking_codes_require_value_detection(self) -> None:
        with pytest.raises(ValidationError, match="VALUE_DETECTION_ENABLED"):
            FeatureFlags(booking_codes_enabled=True, _env_file=None)  # type: ignore[call-arg]


class TestSecretRedaction:
    """Connection strings must never leak credentials into logs."""

    def test_postgres_safe_dsn_hides_password(self) -> None:
        pg = PostgresSettings(password="hunter2", host="db", _env_file=None)  # type: ignore[call-arg]
        assert "hunter2" not in pg.safe_dsn
        assert "***" in pg.safe_dsn
        assert "hunter2" in pg.dsn

    def test_redis_safe_dsn_hides_password(self) -> None:
        cache = RedisSettings(password="s3cret", _env_file=None)  # type: ignore[call-arg]
        assert "s3cret" not in cache.safe_dsn

    def test_settings_repr_does_not_expose_password(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        settings.postgres = PostgresSettings(password="leakme", _env_file=None)  # type: ignore[call-arg]
        assert "leakme" not in repr(settings)


class TestRedisNamespacing:
    """Keys must be prefixed so environments cannot collide in a shared Redis."""

    def test_namespaced_key(self) -> None:
        cache = RedisSettings(key_prefix="qs-staging", _env_file=None)  # type: ignore[call-arg]
        assert cache.namespaced("heartbeat", "worker") == "qs-staging:heartbeat:worker"


class TestObservabilityValidation:
    """A heartbeat TTL at or below the interval guarantees false alarms."""

    def test_ttl_must_exceed_double_interval(self) -> None:
        with pytest.raises(ValidationError, match="heartbeat_ttl_seconds"):
            ObservabilitySettings(
                heartbeat_interval_seconds=30,
                heartbeat_ttl_seconds=30,
                _env_file=None,  # type: ignore[call-arg]
            )


class TestRuntimeValidation:
    """Runtime checks catch unsafe deployments before traffic arrives."""

    def test_production_requires_postgres_password(self) -> None:
        settings = Settings(environment=Environment.PRODUCTION, _env_file=None)  # type: ignore[call-arg]
        with pytest.raises(RuntimeError, match="POSTGRES__PASSWORD"):
            settings.validate_runtime()

    def test_bot_requires_telegram_token(self) -> None:
        settings = Settings(service_role=ServiceRole.BOT, _env_file=None)  # type: ignore[call-arg]
        with pytest.raises(RuntimeError, match="TELEGRAM__BOT_TOKEN"):
            settings.validate_runtime()

    def test_development_api_passes(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        settings.validate_runtime()


class TestEnvironmentLoading:
    """Nested settings load from double-underscore environment variables."""

    def test_nested_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES__HOST", "pgbouncer")
        monkeypatch.setenv("POSTGRES__PORT", "6432")
        monkeypatch.setenv("FEATURES__RESEARCH_MODE", "false")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.postgres.host == "pgbouncer"
        assert settings.postgres.port == 6432
        assert settings.features.research_mode is False

    def test_admin_ids_accept_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM__ADMIN_IDS", "111,222, 333")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.telegram.admin_ids == [111, 222, 333]

    def test_admin_ids_survive_trailing_env_file_comment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: Compose can pass a trailing comment through as a value.

        An empty `TELEGRAM__ADMIN_IDS=  # note` line in an env_file reached the
        application as the literal comment text and crashed all three services
        at startup.
        """
        monkeypatch.setenv("TELEGRAM__ADMIN_IDS", "   # comma-separated numeric Telegram user IDs")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.telegram.admin_ids == []

    def test_admin_ids_ignore_comment_after_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM__ADMIN_IDS", "111,222 # my admins")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.telegram.admin_ids == [111, 222]

    def test_admin_ids_reject_real_garbage_with_clear_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TELEGRAM__ADMIN_IDS", "111,not-an-id")
        with pytest.raises(ValidationError, match="TELEGRAM__ADMIN_IDS"):
            Settings(_env_file=None)  # type: ignore[call-arg]


class TestEnvTemplate:
    """The shipped .env.example must be parseable by Docker Compose."""

    def test_no_inline_comments_after_values(self) -> None:
        """Compose may not strip `KEY=value  # note`; keep comments on own lines."""
        from pathlib import Path

        template = Path(__file__).resolve().parents[1] / ".env.example"
        offenders = [
            line
            for line in template.read_text().splitlines()
            if not line.lstrip().startswith("#") and "=" in line and "#" in line
        ]
        assert offenders == [], (
            "Inline comments in .env.example can be passed through as literal "
            f"values by Docker Compose: {offenders}"
        )
