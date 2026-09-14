"""Bot middleware.

Order matters and is fixed in ``app.bot.main``::

    ContextMiddleware   → correlation ID for every update
    DatabaseMiddleware  → one session and one transaction per update
    UserMiddleware      → registers or loads the user
    AcceptanceMiddleware→ blocks everything until terms are accepted

The gate is enforced here rather than by hiding buttons. A Telegram client can
send any callback data it likes, including data for a button it was never
shown, so hiding is presentation and middleware is enforcement.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiogram import BaseMiddleware, Bot
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from aiogram.types import User as TgUser

from app.bot.formatting import TELEGRAM_LIMIT, fit
from app.core.logging import get_logger, set_correlation_id
from app.core.terms import GATE_BLOCKED_MESSAGE
from app.database.models import User
from app.infrastructure.database import Database
from app.services.user_service import UserService, has_valid_acceptance

logger = get_logger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

# The only routes reachable before acceptance. Deliberately minimal:
# /start begins the flow, /help is informational, and the terms callbacks are
# the flow itself. Nothing here exposes analysis, credits or booking.
GATE_EXEMPT_COMMANDS: Final[frozenset[str]] = frozenset({"/start", "/help", "/terms"})
GATE_EXEMPT_CALLBACK_PREFIXES: Final[tuple[str, ...]] = ("terms:",)

BLOCKED_USER_MESSAGE: Final[str] = (
    "This account has been suspended. Contact support if you believe this is " "an error."
)


def _command_of(message: Message) -> str | None:
    """Return the bare command in a message, or ``None``.

    Strips any ``@botname`` suffix so that ``/start@QuantSportBot`` in a group
    is treated as ``/start``.
    """
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return None
    token = text.split(maxsplit=1)[0]
    return token.split("@", 1)[0].lower()


def _is_exempt(event: TelegramObject) -> bool:
    """Return whether this event may proceed without acceptance."""
    if isinstance(event, Message):
        command = _command_of(event)
        return command is not None and command in GATE_EXEMPT_COMMANDS
    if isinstance(event, CallbackQuery):
        data = event.data or ""
        return data.startswith(GATE_EXEMPT_CALLBACK_PREFIXES)
    return False


class ContextMiddleware(BaseMiddleware):
    """Bind a fresh correlation ID to every update."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        correlation_id = set_correlation_id(None)
        data["correlation_id"] = correlation_id
        return await handler(event, data)


class DatabaseMiddleware(BaseMiddleware):
    """Provide one session per update, committed on success.

    A single transaction per update means a handler that fails part-way leaves
    no half-written state — an acceptance is either fully recorded or not at
    all.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self._database.session() as session:
            data["session"] = session
            return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Register or load the user behind an update.

    Updates with no identifiable user (channel posts, edited service messages)
    are dropped: there is nobody to attribute them to and nothing useful to do.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None

        service = UserService(data["session"])
        user, created = await service.register_or_touch(
            telegram_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            language_code=tg_user.language_code,
        )
        data["user"] = user
        data["user_is_new"] = created
        return await handler(event, data)


class AcceptanceMiddleware(BaseMiddleware):
    """Block every route until age and terms have been confirmed.

    Applies to messages and callback queries alike. A callback query is
    answered even when blocked, otherwise the client shows a spinner until it
    times out.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("user")
        if user is None:
            return None

        if user.is_blocked or not user.is_active:
            await self._refuse(event, BLOCKED_USER_MESSAGE)
            logger.info("gate.blocked_user", telegram_id=user.telegram_id)
            return None

        if has_valid_acceptance(user):
            return await handler(event, data)

        if _is_exempt(event):
            return await handler(event, data)

        await self._refuse(event, GATE_BLOCKED_MESSAGE)
        logger.info(
            "gate.rejected",
            telegram_id=user.telegram_id,
            event_type=type(event).__name__,
            accepted_version=user.accepted_terms_version,
        )
        return None

    @staticmethod
    async def _refuse(event: TelegramObject, text: str) -> None:
        """Tell the user why nothing happened, without leaking internals."""
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)


__all__ = [
    "GATE_EXEMPT_CALLBACK_PREFIXES",
    "GATE_EXEMPT_COMMANDS",
    "AcceptanceMiddleware",
    "ContextMiddleware",
    "DatabaseMiddleware",
    "Update",
    "UserMiddleware",
]


class ErrorMiddleware(BaseMiddleware):
    """Turns an unexpected failure into an answer rather than silence.

    A handler raising inside Telegram produces the worst possible experience:
    the button appears to do nothing, the user taps again, and nothing happens
    again. They cannot tell a broken feature from a slow one, and there is no
    signal that anything went wrong.

    Every failure is therefore caught here, logged with its correlation id, and
    answered with a short apology. The internal detail stays in the logs — a
    stack trace is information about our systems, not something a user needs or
    should see.
    """

    def __init__(self) -> None:
        self._logger = get_logger(__name__)

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Run the handler, answering rather than failing silently."""
        try:
            return await handler(event, data)
        except Exception as exc:
            self._logger.exception(
                "bot.handler_failed",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
                event_type=type(event).__name__,
                callback_data=getattr(event, "data", None),
                handler=getattr(handler, "__name__", None),
            )
            await self._apologise(event)
            return None

    async def _apologise(self, event: TelegramObject) -> None:
        """Tell the user something went wrong, without internal detail."""
        message = (
            "Something went wrong on our side handling that. It has been "
            "logged and we are looking at it. Please try again shortly."
        )
        answer = getattr(event, "answer", None)
        if not callable(answer):
            return

        try:
            if isinstance(event, CallbackQuery) or hasattr(event, "message"):
                # Answering a callback clears the button's loading state, so
                # the interface does not appear frozen.
                await answer("Something went wrong. Please try again.", show_alert=True)
                target = getattr(event, "message", None)
                if isinstance(target, Message):
                    await target.answer(message)
            else:
                await answer(message)
        except Exception:  # noqa: BLE001 - a failure to apologise must not raise
            self._logger.warning("bot.apology_failed")


class LengthMiddleware:
    """Keeps every outgoing message within Telegram's size limit.

    Telegram rejects anything over 4,096 characters outright — it does not
    truncate — so a screen rendering an unbounded list fails completely and the
    user sees an error instead of an answer.

    Individual screens should bound their own content, and they do. This exists
    because "should" is not a guarantee: a day with a hundred selections, an
    unusually long club name, a competition list that grows. Trimming at the
    boundary means the worst case is a shortened message rather than a broken
    one.

    Implemented as a session middleware so it intercepts the outgoing API call
    itself. Wrapping the message object instead would mean patching aiogram's
    own methods, which is fragile and breaks as soon as a screen sends through
    a path nobody remembered to wrap.
    """

    def __init__(self) -> None:
        self._logger = get_logger(__name__)

    async def __call__(
        self,
        make_request: Callable[[Bot, TelegramMethod[Any]], Awaitable[Any]],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        """Trim the payload of any method that carries text."""
        text = getattr(method, "text", None)
        if isinstance(text, str) and len(text) > TELEGRAM_LIMIT:
            self._logger.warning(
                "bot.message_trimmed",
                length=len(text),
                method=type(method).__name__,
            )
            method.text = fit(  # type: ignore[attr-defined]
                text, "Trimmed to fit. The full record is on the website."
            )
        return await make_request(bot, method)
