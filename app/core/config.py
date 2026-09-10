"""Environment-driven application configuration.

All configuration enters the application here and nowhere else. No module
should ever read ``os.environ`` directly (the sole exception is
``app.core.version.build_sha``, which reads an image-baked build label).

Nested settings use a double-underscore delimiter, e.g.::

    POSTGRES__HOST=db
    REDIS__PORT=6379
    FEATURES__VALUE_DETECTION_ENABLED=false

Secrets are never given defaults in production. ``Settings.validate_runtime``
enforces that, plus the model-promotion gate agreed in the architecture review.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationInfo, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    """Deployment environment."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class ServiceRole(StrEnum):
    """Which process this interpreter is running as.

    The three roles share one image and one settings object but have different
    responsibilities and different readiness semantics.
    """

    API = "api"
    BOT = "bot"
    WORKER = "worker"


class PostgresSettings(BaseSettings):
    """PostgreSQL connection settings."""

    model_config = SettingsConfigDict(env_prefix="POSTGRES__", extra="ignore")

    host: str = "localhost"
    port: int = 5432
    user: str = "quantsport"
    password: SecretStr = SecretStr("")
    database: str = "quantsport"

    pool_size: int = Field(default=5, ge=1, le=50)
    max_overflow: int = Field(default=10, ge=0, le=50)
    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    pool_recycle_seconds: int = Field(default=1800, gt=0)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    echo_sql: bool = False

    @computed_field(repr=False)  # type: ignore[prop-decorator]
    @property
    def dsn(self) -> str:
        """Async SQLAlchemy DSN. Excluded from repr: it embeds the password."""
        return (
            f"postgresql+asyncpg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def safe_dsn(self) -> str:
        """Password-redacted DSN, safe for logs and health payloads."""
        return f"postgresql+asyncpg://{self.user}:***@{self.host}:{self.port}/{self.database}"


class RedisSettings(BaseSettings):
    """Redis connection settings.

    Redis is a hard dependency, not an optional cache. It carries job state,
    rate limits, distributed locks and cross-process heartbeats.
    """

    model_config = SettingsConfigDict(env_prefix="REDIS__", extra="ignore")

    host: str = "localhost"
    port: int = 6379
    database: int = Field(default=0, ge=0, le=15)
    password: SecretStr | None = None

    socket_timeout_seconds: float = Field(default=3.0, gt=0)
    socket_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    max_connections: int = Field(default=20, ge=1)
    key_prefix: str = "quantsport"

    @computed_field(repr=False)  # type: ignore[prop-decorator]
    @property
    def dsn(self) -> str:
        """Redis DSN including credentials. Excluded from repr."""
        auth = f":{self.password.get_secret_value()}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.database}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def safe_dsn(self) -> str:
        """Password-redacted Redis DSN, safe for logs."""
        auth = ":***@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.database}"

    def namespaced(self, *parts: str) -> str:
        """Build a prefixed Redis key so environments never collide."""
        return ":".join((self.key_prefix, *parts))


class TelegramSettings(BaseSettings):
    """Telegram bot settings.

    Phase 1 only needs the token to prove the bot process can authenticate and
    shut down cleanly. No user-facing handlers exist yet.
    """

    model_config = SettingsConfigDict(env_prefix="TELEGRAM__", extra="ignore")

    bot_token: SecretStr = SecretStr("")
    parse_mode: Literal["HTML", "MarkdownV2"] = "HTML"
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    drop_pending_updates: bool = True
    request_timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        """Parse a comma-separated string so the .env file stays readable.

        Env-file parsers disagree about trailing comments: some strip
        ``KEY=value  # note``, others pass the comment through as part of the
        value. Rather than failing with an opaque ``int()`` error, drop any
        trailing comment and report anything still unparseable by name.

        Raises:
            ValueError: If a non-comment fragment is not an integer.
        """
        if not isinstance(value, str):
            return value

        text = value.split("#", 1)[0].strip()
        if not text:
            return []

        ids: list[int] = []
        for part in text.split(","):
            token = part.strip()
            if not token:
                continue
            try:
                ids.append(int(token))
            except ValueError:
                raise ValueError(
                    f"TELEGRAM__ADMIN_IDS contains a non-numeric entry: {token!r}. "
                    "Expected comma-separated numeric Telegram user IDs, "
                    "e.g. 12345678,87654321."
                ) from None
        return ids

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_configured(self) -> bool:
        """True when a non-empty bot token has been supplied."""
        return bool(self.bot_token.get_secret_value())


class FeatureFlags(BaseSettings):
    """Feature flags.

    These encode the governance decisions from the architecture review as
    executable rules rather than documentation:

    * ``research_mode`` is on and ``value_detection_enabled`` is off by default.
    * ``value_detection_enabled`` cannot be turned on without naming the model
      version that passed the validation gate.
    """

    model_config = SettingsConfigDict(env_prefix="FEATURES__", extra="ignore")

    research_mode: bool = True
    """Research surfaces (metrics, calibration reports) are available."""

    promoted_model_version: str | None = None
    """Identifier of the model version that passed the Phase 8B gate.

    Declared before ``value_detection_enabled`` because the validator below
    reads it from ``info.data``, which only contains earlier fields.
    """

    value_detection_enabled: bool = False
    """User-facing value/edge output. Gated on model promotion."""

    booking_codes_enabled: bool = False
    """Booking-code generation. Requires value detection to be live."""

    payments_enabled: bool = False
    """Payment processing. Requires a verified payment provider agreement."""

    scheduled_odds_capture_enabled: bool = False
    """Continuous odds polling for opening/closing snapshots. Phase 6+."""

    @field_validator("value_detection_enabled")
    @classmethod
    def _require_promoted_model(cls, value: bool, info: ValidationInfo) -> bool:
        """Refuse to serve value detection without a promoted model version."""
        if value and not info.data.get("promoted_model_version"):
            raise ValueError(
                "FEATURES__VALUE_DETECTION_ENABLED cannot be true without "
                "FEATURES__PROMOTED_MODEL_VERSION. User-facing edge output is "
                "gated on a model that passed backtest validation (Phase 8B)."
            )
        return value

    @field_validator("booking_codes_enabled")
    @classmethod
    def _require_value_detection(cls, value: bool, info: ValidationInfo) -> bool:
        """Booking codes are meaningless without selections to book."""
        if value and not info.data.get("value_detection_enabled"):
            raise ValueError(
                "FEATURES__BOOKING_CODES_ENABLED requires " "FEATURES__VALUE_DETECTION_ENABLED."
            )
        return value


class ObservabilitySettings(BaseSettings):
    """Logging and health-check settings."""

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY__", extra="ignore")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_sql_queries: bool = False

    heartbeat_interval_seconds: int = Field(default=15, ge=1)
    """How often the bot and worker refresh their Redis heartbeat key."""

    heartbeat_ttl_seconds: int = Field(default=60, ge=2)
    """Heartbeat key expiry. Must exceed the interval with margin to spare."""

    readiness_timeout_seconds: float = Field(default=3.0, gt=0)
    """Per-dependency timeout when answering a readiness probe."""

    @field_validator("heartbeat_ttl_seconds")
    @classmethod
    def _ttl_exceeds_interval(cls, value: int, info: ValidationInfo) -> int:
        """A TTL below the refresh interval guarantees false 'down' alerts."""
        interval = info.data.get("heartbeat_interval_seconds", 15)
        if value <= interval * 2:
            raise ValueError(
                "heartbeat_ttl_seconds must be more than twice "
                "heartbeat_interval_seconds to tolerate one missed refresh."
            )
        return value


class Settings(BaseSettings):
    """Root application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "QUANTSPORT AI"
    environment: Environment = Environment.DEVELOPMENT
    service_role: ServiceRole = ServiceRole.API
    debug: bool = False

    api_host: str = "0.0.0.0"  # noqa: S104 - containers must bind all interfaces
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_root_path: str = ""

    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        """True in the production environment."""
        return self.environment is Environment.PRODUCTION

    def validate_runtime(self) -> None:
        """Assert settings that only matter once a real process starts.

        Kept separate from field validation so that unit tests can build a
        ``Settings`` object without supplying every production secret.

        Raises:
            RuntimeError: If the configuration is unsafe for this role or
                environment.
        """
        problems: list[str] = []

        if self.is_production:
            if not self.postgres.password.get_secret_value():
                problems.append("POSTGRES__PASSWORD must be set in production.")
            if self.debug:
                problems.append("DEBUG must be false in production.")
            if self.observability.log_format != "json":
                problems.append("OBSERVABILITY__LOG_FORMAT must be 'json' in production.")

        if self.service_role is ServiceRole.BOT and not self.telegram.is_configured:
            problems.append("TELEGRAM__BOT_TOKEN is required for the bot service.")

        if problems:
            raise RuntimeError(
                "Invalid configuration for role "
                f"'{self.service_role}' in '{self.environment}':\n  - " + "\n  - ".join(problems)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that every caller sees the same object. Tests that need a
    different configuration should call ``get_settings.cache_clear()``.
    """
    return Settings()
