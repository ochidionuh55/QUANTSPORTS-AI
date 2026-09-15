"""Community feature tests.

Two rules. Aggregate counts only — a milestone is worth celebrating, the people
making it up are not a leaderboard. And the channel must never become a wall: a
membership check that cannot reach Telegram has to fail open, or an outage locks
out every user at once.
"""

from __future__ import annotations

from app.core.community import (
    CHANNEL_ID,
    CHANNEL_NAME,
    CHANNEL_URL,
    CHANNEL_USERNAME,
    JOIN_PROMPT,
)


class TestChannelDefinition:
    """One definition, used everywhere."""

    def test_url_matches_username(self) -> None:
        assert f"https://t.me/{CHANNEL_USERNAME}" == CHANNEL_URL

    def test_id_is_the_form_telegram_expects(self) -> None:
        """Membership lookups need the @ form, links need the URL form."""
        assert f"@{CHANNEL_USERNAME}" == CHANNEL_ID

    def test_channel_is_named(self) -> None:
        assert CHANNEL_NAME

    def test_prompt_mentions_the_losses(self) -> None:
        """A channel sold as wins only would misrepresent what is posted
        there."""
        assert "did not work" in JOIN_PROMPT


class TestCommunityScreen:
    """What the bot shows."""

    def test_counts_are_rendered(self) -> None:
        from types import SimpleNamespace

        from app.bot.formatting import format_community

        stats = SimpleNamespace(
            members=1240,
            active_this_week=310,
            selections_published=8421,
            days_on_record=12,
        )
        rendered = format_community(stats, CHANNEL_URL)

        assert "1,240" in rendered
        assert "8,421" in rendered

    def test_no_identifiers_are_shown(self) -> None:
        """Counts, never names."""
        from types import SimpleNamespace

        from app.bot.formatting import format_community

        stats = SimpleNamespace(
            members=5,
            active_this_week=2,
            selections_published=10,
            days_on_record=1,
        )
        rendered = format_community(stats, CHANNEL_URL).lower()

        for word in ("username", "telegram_id", "@user", "last seen"):
            assert word not in rendered

    def test_states_the_privacy_position(self) -> None:
        from types import SimpleNamespace

        from app.bot.formatting import format_community

        stats = SimpleNamespace(
            members=0, active_this_week=0, selections_published=0, days_on_record=0
        )
        assert "never names" in format_community(stats, CHANNEL_URL)

    def test_empty_product_does_not_crash(self) -> None:
        from types import SimpleNamespace

        from app.bot.formatting import format_community

        stats = SimpleNamespace(
            members=0, active_this_week=0, selections_published=0, days_on_record=0
        )
        assert format_community(stats, CHANNEL_URL)


class TestMembershipFailsOpen:
    """An unknown answer must never be treated as "not joined"."""

    async def test_unreachable_telegram_returns_unknown(self) -> None:
        from types import SimpleNamespace

        from app.bot.handlers import _is_member

        class _Bot:
            async def get_chat_member(self, chat: str, user: int) -> object:
                raise RuntimeError("telegram unavailable")

        callback = SimpleNamespace(bot=_Bot())
        user = SimpleNamespace(telegram_id=1)

        assert await _is_member(callback, user) is None  # type: ignore[arg-type]

    async def test_member_is_recognised(self) -> None:
        from types import SimpleNamespace

        from app.bot.handlers import _is_member

        class _Bot:
            async def get_chat_member(self, chat: str, user: int) -> object:
                return SimpleNamespace(status="member")

        callback = SimpleNamespace(bot=_Bot())
        user = SimpleNamespace(telegram_id=1)

        assert await _is_member(callback, user) is True  # type: ignore[arg-type]

    async def test_non_member_is_recognised(self) -> None:
        from types import SimpleNamespace

        from app.bot.handlers import _is_member

        class _Bot:
            async def get_chat_member(self, chat: str, user: int) -> object:
                return SimpleNamespace(status="left")

        callback = SimpleNamespace(bot=_Bot())
        user = SimpleNamespace(telegram_id=1)

        assert await _is_member(callback, user) is False  # type: ignore[arg-type]

    async def test_missing_bot_returns_unknown(self) -> None:
        from types import SimpleNamespace

        from app.bot.handlers import _is_member

        callback = SimpleNamespace(bot=None)
        user = SimpleNamespace(telegram_id=1)

        assert await _is_member(callback, user) is None  # type: ignore[arg-type]
