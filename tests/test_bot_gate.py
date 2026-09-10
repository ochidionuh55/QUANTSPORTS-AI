"""Bot gate and menu tests.

The centre of gravity is bypass: a Telegram client can send any callback data
it chooses, including data for a button it was never shown. Hiding a button is
presentation; the middleware is the control. These tests attack the middleware
directly rather than going through the UI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.keyboards import main_menu, menu_features
from app.bot.middleware import (
    BLOCKED_USER_MESSAGE,
    AcceptanceMiddleware,
    UserMiddleware,
)
from app.core.config import FeatureFlags
from app.core.terms import (
    CONFIRM_CALLBACK_DATA,
    GATE_BLOCKED_MESSAGE,
    TERMS_VERSION,
)
from app.database.base import Base
from app.database.models import User
from app.services.user_service import UserService, has_valid_acceptance


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Provide a session against a fresh in-memory schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as active:
        yield active
    await engine.dispose()


class FakeMessage:
    """Stands in for aiogram's Message."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []

    async def answer(self, text: str, **_kwargs: Any) -> None:
        self.replies.append(text)


class FakeCallback:
    """Stands in for aiogram's CallbackQuery."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[str, bool]] = []
        self.message = None

    async def answer(self, text: str = "", show_alert: bool = False, **_k: Any) -> None:
        self.answers.append((text, show_alert))


class FakeTelegramUser:
    """Stands in for aiogram's User."""

    def __init__(self, user_id: int = 555, is_bot: bool = False) -> None:
        self.id = user_id
        self.is_bot = is_bot
        self.username = "tester"
        self.first_name = "Test"
        self.language_code = "en"


@pytest.fixture(autouse=True)
def _register_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Teach the middleware's type checks to accept the fakes.

    aiogram's models are pydantic, not ABCs, so they cannot be registered as
    virtual base classes. Constructing real Message and CallbackQuery objects
    would mean supplying a dozen irrelevant required fields, which would
    obscure what each test is actually asserting.
    """
    import app.bot.middleware as middleware_module

    real_isinstance = isinstance

    def patched(obj: object, expected: Any) -> bool:
        if expected is middleware_module.Message:
            return real_isinstance(obj, FakeMessage) or real_isinstance(obj, expected)
        if expected is middleware_module.CallbackQuery:
            return real_isinstance(obj, FakeCallback) or real_isinstance(obj, expected)
        return real_isinstance(obj, expected)

    monkeypatch.setattr(middleware_module, "isinstance", patched, raising=False)


async def _make_user(
    session: AsyncSession, telegram_id: int = 555, accepted: bool = False, **kwargs: Any
) -> User:
    """Insert a user, optionally with a valid acceptance record."""
    user = User(telegram_id=telegram_id, credits=0, **kwargs)
    if accepted:
        now = datetime.now(UTC)
        user.age_confirmed_at = now
        user.terms_accepted_at = now
        user.accepted_terms_version = TERMS_VERSION
    session.add(user)
    await session.flush()
    return user


async def _run_gate(event: Any, user: User) -> bool:
    """Run the acceptance middleware; return whether the handler was reached."""
    reached = False

    async def handler(_event: Any, _data: dict[str, Any]) -> None:
        nonlocal reached
        reached = True

    await AcceptanceMiddleware()(handler, event, {"user": user})
    return reached


class TestAcceptanceState:
    """Acceptance requires age, terms and a current version."""

    async def test_new_user_has_no_acceptance(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        assert not has_valid_acceptance(user)

    async def test_recording_acceptance_grants_access(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        await UserService(session).record_acceptance(user)

        assert has_valid_acceptance(user)
        assert user.age_confirmed_at is not None
        assert user.terms_accepted_at is not None
        assert user.accepted_terms_version == TERMS_VERSION

    async def test_outdated_version_invalidates_acceptance(self, session: AsyncSession) -> None:
        """A material terms change must force re-acceptance."""
        user = await _make_user(session, accepted=True)
        assert has_valid_acceptance(user)

        assert not has_valid_acceptance(user, required_version="2.0")

    async def test_partial_acceptance_is_not_acceptance(self, session: AsyncSession) -> None:
        """An age confirmation alone must not unlock the bot."""
        user = await _make_user(session)
        user.age_confirmed_at = datetime.now(UTC)
        user.accepted_terms_version = TERMS_VERSION
        assert not has_valid_acceptance(user)


class TestGateBlocksUnacceptedUsers:
    """Nothing but the exempt routes may run before acceptance."""

    async def test_blocks_arbitrary_message(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        event = FakeMessage("show me today's matches")

        assert await _run_gate(event, user) is False
        assert event.replies == [GATE_BLOCKED_MESSAGE]

    async def test_blocks_unexempt_command(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        event = FakeMessage("/scan")

        assert await _run_gate(event, user) is False

    async def test_allows_start(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        assert await _run_gate(FakeMessage("/start"), user) is True

    async def test_allows_help_and_terms(self, session: AsyncSession) -> None:
        user = await _make_user(session)
        assert await _run_gate(FakeMessage("/help"), user) is True
        assert await _run_gate(FakeMessage("/terms"), user) is True

    async def test_allows_command_with_bot_suffix(self, session: AsyncSession) -> None:
        """/start@BotName in a group must be recognised as /start."""
        user = await _make_user(session)
        assert await _run_gate(FakeMessage("/start@QuantSportBot"), user) is True

    async def test_accepted_user_passes(self, session: AsyncSession) -> None:
        user = await _make_user(session, accepted=True)
        assert await _run_gate(FakeMessage("anything at all"), user) is True


class TestCallbackQueryBypassAttempts:
    """Callback data is attacker-controlled and must be gated identically."""

    async def test_blocks_unaccepted_callback(self, session: AsyncSession) -> None:
        """The core bypass: send a menu callback that was never displayed."""
        user = await _make_user(session)
        event = FakeCallback("menu:scan")

        assert await _run_gate(event, user) is False
        assert event.answers[0][0] == GATE_BLOCKED_MESSAGE
        assert event.answers[0][1] is True

    @pytest.mark.parametrize(
        "payload",
        [
            "menu:main",
            "menu:account",
            "menu:booking",
            "menu:credits",
            "menu:predictions",
            "admin:grant_credits",
            "",
        ],
    )
    async def test_no_menu_callback_slips_through(
        self, session: AsyncSession, payload: str
    ) -> None:
        user = await _make_user(session)
        assert await _run_gate(FakeCallback(payload), user) is False

    async def test_allows_terms_callbacks(self, session: AsyncSession) -> None:
        """The acceptance flow itself must be reachable while blocked."""
        user = await _make_user(session)
        assert await _run_gate(FakeCallback(CONFIRM_CALLBACK_DATA), user) is True

    async def test_blocked_callback_is_answered(self, session: AsyncSession) -> None:
        """An unanswered callback leaves a spinner on the client."""
        user = await _make_user(session)
        event = FakeCallback("menu:scan")
        await _run_gate(event, user)
        assert event.answers, "callback query must always be answered"


class TestBlockedUsers:
    """Suspension outranks acceptance."""

    async def test_blocked_user_cannot_act(self, session: AsyncSession) -> None:
        user = await _make_user(session, accepted=True, is_blocked=True)
        event = FakeMessage("/start")

        assert await _run_gate(event, user) is False
        assert event.replies == [BLOCKED_USER_MESSAGE]

    async def test_inactive_user_cannot_act(self, session: AsyncSession) -> None:
        user = await _make_user(session, accepted=True, is_active=False)
        assert await _run_gate(FakeMessage("/start"), user) is False

    async def test_blocked_user_cannot_use_exempt_route(self, session: AsyncSession) -> None:
        """Suspension must not be escapable via /start."""
        user = await _make_user(session, accepted=True, is_blocked=True)
        assert await _run_gate(FakeCallback(CONFIRM_CALLBACK_DATA), user) is False

    async def test_missing_user_is_dropped(self) -> None:
        assert await _run_gate(FakeMessage("/start"), None) is False  # type: ignore[arg-type]


class TestRegistration:
    """Registration creates a record but confers no access."""

    async def test_registers_new_user(self, session: AsyncSession) -> None:
        service = UserService(session)
        user, created = await service.register_or_touch(999, "handle", "Name", "en")

        assert created
        assert user.telegram_id == 999
        assert user.credits == 0
        assert not has_valid_acceptance(user)

    async def test_second_call_does_not_duplicate(self, session: AsyncSession) -> None:
        service = UserService(session)
        first, created_first = await service.register_or_touch(999)
        second, created_second = await service.register_or_touch(999)

        assert created_first and not created_second
        assert first.id == second.id

    async def test_profile_changes_are_refreshed(self, session: AsyncSession) -> None:
        service = UserService(session)
        await service.register_or_touch(999, username="old")
        user, _ = await service.register_or_touch(999, username="new")
        assert user.username == "new"

    async def test_bot_accounts_are_ignored(self, session: AsyncSession) -> None:
        reached = False

        async def handler(_event: Any, _data: dict[str, Any]) -> None:
            nonlocal reached
            reached = True

        await UserMiddleware()(
            handler,
            FakeMessage("/start"),
            {"session": session, "event_from_user": FakeTelegramUser(is_bot=True)},
        )
        assert not reached


class TestFeatureAwareMenu:
    """The menu must not advertise functionality that does not exist."""

    def test_unreleased_features_are_hidden(self) -> None:
        flags = FeatureFlags(_env_file=None)  # type: ignore[call-arg]
        labels = {button.text for row in main_menu(flags).inline_keyboard for button in row}
        assert "Value selections" not in labels
        assert "My predictions" not in labels
        assert "Generate booking code" not in labels
        assert "Buy credits" not in labels

    def test_available_features_are_shown(self) -> None:
        flags = FeatureFlags(_env_file=None)  # type: ignore[call-arg]
        labels = {button.text for row in main_menu(flags).inline_keyboard for button in row}
        assert "👤 My account" in labels
        assert "📖 How it works" in labels
        assert "⚽ Today's analysis" in labels

    def test_menu_is_never_empty(self) -> None:
        flags = FeatureFlags(_env_file=None)  # type: ignore[call-arg]
        assert main_menu(flags).inline_keyboard

    def test_payment_flag_controls_credits_button(self) -> None:
        flags = FeatureFlags(
            promoted_model_version="v1",
            value_detection_enabled=True,
            payments_enabled=True,
            _env_file=None,  # type: ignore[call-arg]
        )
        labels = {button.text for row in main_menu(flags).inline_keyboard for button in row}
        assert "Buy credits" in labels

    def test_every_feature_declares_its_phase(self) -> None:
        flags = FeatureFlags(_env_file=None)  # type: ignore[call-arg]
        for feature in menu_features(flags):
            assert feature.phase >= 3
            assert feature.callback_data.startswith("menu:")
