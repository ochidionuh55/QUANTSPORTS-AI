"""Message length tests.

Telegram rejects anything over 4,096 characters outright rather than
truncating, so an unbounded list does not produce a long message — it produces
no message and an error the user cannot act on. This is what took the history
screen down with a hundred published selections.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.bot.formatting import (
    SAFE_LIMIT,
    TELEGRAM_LIMIT,
    fit,
    format_history_service,
)
from app.bot.middleware import LengthMiddleware

NOW = datetime(2026, 9, 13, 18, tzinfo=UTC)


class _SilentLogger:
    """Swallows log calls so a captured stream cannot fail a test."""

    def warning(self, *args: object, **kwargs: object) -> None:
        """Discard."""


def _selection(index: int) -> SimpleNamespace:
    """One published selection, as the history view renders it."""
    return SimpleNamespace(
        service_label="🏆 Best Home Win",
        home_name=f"Alpha Football Club {index}",
        away_name=f"Bravo Athletic Association {index}",
        competition="Premier League",
        kickoff=NOW,
        outcome="Home",
        probability=0.72,
        coverage="fully_modelled",
        status="won",
        home_goals=2,
        away_goals=0,
    )


class TestFit:
    """Trimming as a last line of defence."""

    def test_short_text_is_untouched(self) -> None:
        assert fit("short") == "short"

    def test_long_text_is_brought_under_the_limit(self) -> None:
        trimmed = fit("x" * 9000, "Trimmed.")
        assert len(trimmed) <= SAFE_LIMIT

    def test_trimmed_text_carries_the_note(self) -> None:
        """A silently shortened message is worse than one that says so."""
        assert "Trimmed." in fit("x" * 9000, "Trimmed.")

    def test_cuts_on_a_line_break(self) -> None:
        """Cutting mid-tag would leave broken HTML that Telegram also
        rejects."""
        text = "\n".join(f"<b>line {i}</b>" for i in range(2000))
        trimmed = fit(text, "Trimmed.")
        assert trimmed.count("<b>") == trimmed.count("</b>")


class TestHistoryService:
    """The screen that failed.

    Now split per service, which bounds it naturally — but a single busy
    service can still publish more than a message will hold.
    """

    def test_a_hundred_selections_still_fit(self) -> None:
        """The real failure: a day with a hundred published selections
        produced a message Telegram refused outright."""
        rendered = format_history_service(NOW.date(), [_selection(i) for i in range(100)])
        assert len(rendered) < TELEGRAM_LIMIT

    def test_remaining_count_is_stated(self) -> None:
        """A truncated list must say it is truncated, or a reader believes
        they have seen everything."""
        rendered = format_history_service(NOW.date(), [_selection(i) for i in range(40)])
        assert "26 more" in rendered

    def test_short_list_shows_everything(self) -> None:
        rendered = format_history_service(NOW.date(), [_selection(i) for i in range(3)])
        assert "more" not in rendered.split("Published before")[0]


class TestLengthMiddleware:
    """The guard at the boundary."""

    async def test_oversized_payload_is_trimmed(self) -> None:
        middleware = LengthMiddleware()
        # Silenced because the warning writes to a stream pytest closes between
        # modules; the behaviour under test is the trimming, not the log.
        middleware._logger = _SilentLogger()  # type: ignore[assignment]
        method = SimpleNamespace(text="x" * 9000)
        sent: list[str] = []

        async def make_request(bot: object, payload: object) -> str:
            sent.append(payload.text)  # type: ignore[attr-defined]
            return "ok"

        await middleware(make_request, object(), method)  # type: ignore[arg-type]

        assert len(sent[0]) <= SAFE_LIMIT

    async def test_normal_payload_passes_through_unchanged(self) -> None:
        middleware = LengthMiddleware()
        method = SimpleNamespace(text="a normal message")
        sent: list[str] = []

        async def make_request(bot: object, payload: object) -> str:
            sent.append(payload.text)  # type: ignore[attr-defined]
            return "ok"

        await middleware(make_request, object(), method)  # type: ignore[arg-type]

        assert sent == ["a normal message"]

    async def test_methods_without_text_are_ignored(self) -> None:
        """Photos, callbacks and the rest must pass untouched."""
        middleware = LengthMiddleware()
        method = SimpleNamespace(photo=b"binary")

        async def make_request(bot: object, payload: object) -> str:
            return "ok"

        result = await middleware(make_request, object(), method)  # type: ignore[arg-type]
        assert result == "ok"

    def test_bot_attaches_the_guard(self) -> None:
        """Attached in the factory so every path is covered, not only the
        ones a screen remembered to wrap."""
        import os

        os.environ["TELEGRAM__BOT_TOKEN"] = "1:test"
        from app.bot.main import build_bot
        from app.core.config import Settings

        bot = build_bot(Settings(_env_file=None))
        assert len(bot.session.middleware._middlewares) >= 1
