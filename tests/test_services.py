"""Service wiring tests.

These are smoke tests: they assert that each of the three processes can be
constructed without touching the network. Their real value is catching
import-time and wiring errors, which otherwise only appear on deploy.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from app.core.config import Environment, ServiceRole, Settings
from app.core.constants import (
    MINIMUM_AGE,
    NO_QUALIFIED_OPPORTUNITY,
    RESPONSIBLE_USE_NOTICE,
)
from app.utils.lifecycle import run_until_shutdown


class TestApiFactory:
    """The API application must build and register its routes."""

    def test_creates_app(self, settings: Settings) -> None:
        from app.main import create_app

        app = create_app(settings)
        assert isinstance(app, FastAPI)
        assert app.state.settings.service_role is ServiceRole.API

    def test_registers_expected_routes(self, settings: Settings) -> None:
        from app.main import create_app

        paths = {route.path for route in create_app(settings).routes}  # type: ignore[attr-defined]
        assert {"/health", "/health/live", "/health/ready", "/system/info"} <= paths

    def test_docs_disabled_in_production(self) -> None:
        from app.main import create_app

        settings = Settings(
            environment=Environment.PRODUCTION,
            _env_file=None,  # type: ignore[call-arg]
        )
        settings.postgres.password = settings.postgres.password.__class__("pw")
        app = create_app(settings)
        assert app.docs_url is None
        assert app.openapi_url is None


class TestBotWiring:
    """The bot process must wire up without contacting Telegram."""

    def test_dispatcher_has_router(self, settings: Settings) -> None:
        from app.bot.main import build_dispatcher
        from app.infrastructure.database import Database

        dispatcher = build_dispatcher(Database(settings), settings)
        assert [r.name for r in dispatcher.sub_routers] == ["phase3"]

    def test_dispatcher_can_be_built_twice(self, settings: Settings) -> None:
        """Regression: a module-level router could only ever attach once."""
        from app.bot.main import build_dispatcher
        from app.infrastructure.database import Database

        build_dispatcher(Database(settings), settings)
        build_dispatcher(Database(settings), settings)

    def test_gate_middleware_covers_messages_and_callbacks(self, settings: Settings) -> None:
        """The acceptance gate must be registered on both observers.

        Registering it only on messages would leave every callback query
        reachable by any client willing to send the payload.
        """
        from app.bot.main import build_dispatcher
        from app.bot.middleware import AcceptanceMiddleware
        from app.infrastructure.database import Database

        dispatcher = build_dispatcher(Database(settings), settings)
        for observer in (dispatcher.message, dispatcher.callback_query):
            registered = [type(m) for m in observer.middleware]
            assert AcceptanceMiddleware in registered

    def test_bot_client_uses_configured_token(self, settings: Settings) -> None:
        from app.bot.main import build_bot

        settings.telegram.bot_token = settings.telegram.bot_token.__class__("123456:test-token")
        bot = build_bot(settings)
        assert bot.token == "123456:test-token"


class TestWorkerWiring:
    """The worker must register its scheduled jobs."""

    def test_registers_self_check_job(
        self, settings: Settings, fake_database: object, fake_redis: object
    ) -> None:
        from app.worker.main import build_scheduler

        scheduler = build_scheduler(settings, fake_database, fake_redis)  # type: ignore[arg-type]
        job_ids = {job.id for job in scheduler.get_jobs()}
        assert "infrastructure_self_check" in job_ids

    def test_jobs_do_not_overlap(
        self, settings: Settings, fake_database: object, fake_redis: object
    ) -> None:
        from app.worker.main import build_scheduler

        scheduler = build_scheduler(settings, fake_database, fake_redis)  # type: ignore[arg-type]
        for job in scheduler.get_jobs():
            assert job.max_instances == 1
            assert job.coalesce is True


class TestLifecycle:
    """Shutdown must run even when startup fails, or resources leak."""

    async def test_waits_for_signal_after_startup(self) -> None:
        """After startup the service must block, not exit immediately."""
        events: list[str] = []

        async def startup() -> None:
            events.append("start")

        async def shutdown() -> None:
            events.append("stop")

        task = asyncio.create_task(run_until_shutdown("test", startup, shutdown))
        await asyncio.sleep(0.05)

        assert events == ["start"], "service exited without a shutdown signal"
        assert not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_shutdown_runs_when_startup_raises(self) -> None:
        events: list[str] = []

        async def startup() -> None:
            raise RuntimeError("redis unreachable")

        async def shutdown() -> None:
            events.append("stop")

        with pytest.raises(RuntimeError, match="redis unreachable"):
            await run_until_shutdown("test", startup, shutdown)
        assert events == ["stop"]


class TestResponsibleUseText:
    """Compliance text must exist and be correct from Phase 1 onward."""

    def test_notice_states_the_age_limit(self) -> None:
        assert str(MINIMUM_AGE) in RESPONSIBLE_USE_NOTICE

    def test_notice_makes_no_profit_claim(self) -> None:
        lowered = RESPONSIBLE_USE_NOTICE.lower()
        assert "guarantee" in lowered
        for forbidden in ("guaranteed profit", "sure win", "risk-free"):
            assert forbidden not in lowered

    def test_no_opportunity_message_is_stable(self) -> None:
        assert NO_QUALIFIED_OPPORTUNITY == "NO QUALIFIED OPPORTUNITY FOUND."
