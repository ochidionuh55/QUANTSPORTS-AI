"""Telegram bot service entrypoint.

Phase 3 scope: onboarding. Registration, a blocking age and terms gate,
middleware enforcement, a centralised error handler and a feature-aware menu.
No analysis features exist yet and none are advertised.

Run with::

    python -m app.bot.main
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.types import ErrorEvent

from app.bot import handlers
from app.bot.middleware import (
    AcceptanceMiddleware,
    ContextMiddleware,
    DatabaseMiddleware,
    ErrorMiddleware,
    LengthMiddleware,
    UserMiddleware,
)
from app.core.config import ServiceRole, Settings, get_settings
from app.core.logging import configure_logging, get_correlation_id, get_logger
from app.infrastructure.database import Database
from app.infrastructure.heartbeat import HeartbeatWriter
from app.infrastructure.migrations import ensure_schema
from app.infrastructure.redis import RedisClient
from app.utils.lifecycle import run_until_shutdown

logger = get_logger(__name__)

USER_FACING_ERROR = "Something went wrong on our side. Please try again in a moment."


def build_dispatcher(database: Database, settings: Settings) -> Dispatcher:
    """Create the dispatcher with middleware and handlers attached.

    Middleware order is load-bearing. Context first so every later log line
    carries a correlation ID; database next so the user lookup has a session;
    user before acceptance so the gate has somebody to check. Each is
    registered for messages and callback queries alike, because a callback is
    just as capable of reaching a handler as a command.
    """
    dispatcher = Dispatcher()
    dispatcher["settings"] = settings

    for observer in (dispatcher.message, dispatcher.callback_query):
        # Outermost, so a failure anywhere inside still reaches the user as an
        # answer. A button that silently does nothing is the worst outcome:
        # indistinguishable from a slow one, and invisible to whoever could fix
        # it.
        observer.middleware(ErrorMiddleware())
        observer.middleware(ContextMiddleware())
        observer.middleware(DatabaseMiddleware(database))
        observer.middleware(UserMiddleware())
        observer.middleware(AcceptanceMiddleware())

    dispatcher.include_router(handlers.build_router())

    @dispatcher.error()
    async def on_error(event: ErrorEvent) -> bool:
        """Log the failure and show the user a neutral message.

        Stack traces never reach Telegram. The correlation ID is included so a
        user report can be tied to the logged exception.
        """
        logger.exception(
            "bot.handler_failed",
            error_type=type(event.exception).__name__,
            update_id=getattr(event.update, "update_id", None),
        )
        message = getattr(event.update, "message", None)
        callback = getattr(event.update, "callback_query", None)
        try:
            if callback is not None:
                await callback.answer(USER_FACING_ERROR, show_alert=True)
            elif message is not None:
                await message.answer(f"{USER_FACING_ERROR}\n\nReference: {get_correlation_id()}")
        except Exception:  # noqa: BLE001 - never fail inside the error handler
            logger.warning("bot.error_reply_failed")
        return True

    return dispatcher


def build_bot(settings: Settings) -> Bot:
    """Create the aiogram bot client from settings."""
    bot = Bot(
        token=settings.telegram.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=settings.telegram.parse_mode),
    )

    # Attached to the session so it wraps the outgoing API call itself. Doing
    # it here covers every path a screen might send through, rather than only
    # the ones we remembered to wrap.
    bot.session.middleware(LengthMiddleware())
    return bot


async def run_bot(settings: Settings) -> None:
    """Run the bot until a shutdown signal is received."""
    redis = RedisClient(settings)
    database = Database(settings)
    heartbeat = HeartbeatWriter(redis, ServiceRole.BOT, settings)
    bot = build_bot(settings)
    dispatcher = build_dispatcher(database, settings)
    polling_task: asyncio.Task[None] | None = None

    async def startup() -> None:
        nonlocal polling_task
        await database.connect()
        await redis.connect()
        await ensure_schema(settings, database, redis, required=False)
        if not await redis.ping():
            raise RuntimeError("Redis is unreachable; the bot cannot start.")
        await heartbeat.start()

        identity = await bot.get_me()
        logger.info("bot.authenticated", username=identity.username, bot_id=identity.id)

        polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                handle_signals=False,
                drop_pending_updates=settings.telegram.drop_pending_updates,
            ),
            name="telegram-polling",
        )

    async def shutdown() -> None:
        if polling_task is not None:
            await dispatcher.stop_polling()
            polling_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                logger.debug("bot.polling_cancelled")
            except Exception as exc:  # noqa: BLE001 - shutdown must complete
                logger.warning("bot.polling_shutdown_error", error=str(exc))
        await heartbeat.stop()
        await bot.session.close()
        await redis.disconnect()
        await database.disconnect()

    await run_until_shutdown("bot", startup, shutdown)


def main() -> None:
    """Configure the process and run the bot."""
    settings = get_settings()
    settings.service_role = ServiceRole.BOT
    configure_logging(settings)
    settings.validate_runtime()
    asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
