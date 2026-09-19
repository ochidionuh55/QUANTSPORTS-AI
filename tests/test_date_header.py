"""Every dated surface says which date it is showing.

The Outsider Board listed Saturday's fixtures on a Friday for weeks. Nothing
on screen said which day they belonged to, so there was nothing for a reader
to disbelieve. A date header is not decoration: it is what makes a wrong date
visible.

"Today" and an explicitly chosen date must also read differently. A screen
saying "today's card" while showing another day is worse than one saying
nothing, because it asserts something false.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.bot.formatting import format_date_header, format_market_menu

TODAY = date(2026, 9, 19)


class TestTodayIsDistinguishable:
    def test_today_says_today(self) -> None:
        assert format_date_header(TODAY, TODAY).startswith("📅 Today")

    def test_yesterday_says_yesterday(self) -> None:
        header = format_date_header(date(2026, 9, 18), TODAY)
        assert "Yesterday" in header
        assert "Today" not in header

    def test_tomorrow_says_tomorrow(self) -> None:
        header = format_date_header(date(2026, 9, 20), TODAY)
        assert "Tomorrow" in header
        assert "Today" not in header

    def test_a_distant_date_says_only_the_date(self) -> None:
        header = format_date_header(date(2026, 9, 12), TODAY)
        assert "Today" not in header
        assert "Yesterday" not in header
        assert "Sat 12 Sep 2026" in header

    def test_every_header_carries_the_actual_date(self) -> None:
        """Even "Today" states it, so a screenshot is self-describing."""
        for offset in range(-5, 6):
            day = date.fromordinal(TODAY.toordinal() + offset)
            header = format_date_header(day, TODAY)
            assert str(day.day) in header
            assert f"{day:%b}" in header
            assert str(day.year) in header


class TestPortableFormatting:
    """``%-d`` is a glibc extension that raises on Windows."""

    @pytest.mark.parametrize("day_number", [1, 9, 10, 19, 28, 31])
    def test_single_and_double_digit_days_render(self, day_number: int) -> None:
        day = date(2026, 1, day_number)
        header = format_date_header(day, date(2026, 6, 1))
        assert str(day_number) in header

    def test_no_leading_zero_on_single_digit_days(self) -> None:
        """The reason %-d was reached for in the first place."""
        header = format_date_header(date(2026, 9, 5), date(2026, 9, 19))
        assert " 5 Sep" in header
        assert " 05 Sep" not in header


class TestMarketMenuShowsItsDate:
    def test_menu_carries_the_header(self) -> None:
        rendered = format_market_menu({}, TODAY)
        assert "📅" in rendered

    def test_menu_does_not_claim_today_for_another_date(self) -> None:
        """It said "On today's card" regardless of which day was shown."""
        rendered = format_market_menu({"Home win": 6}, date(2026, 9, 12))
        assert "today's card" not in rendered.lower()

    def test_counts_still_render(self) -> None:
        rendered = format_market_menu({"Home win": 6}, TODAY)
        assert "Home win: 6" in rendered


class TestNavigationCarriesTheDate:
    """The date must survive date → market → fixture → back."""

    def test_history_day_callbacks_carry_an_iso_date(self) -> None:
        import inspect

        import app.bot.handlers as handlers

        source = inspect.getsource(handlers)
        assert 'callback_data=f"mh:day:{day.isoformat()}"' in source

    def test_market_within_a_day_carries_that_day(self) -> None:
        import inspect

        import app.bot.handlers as handlers

        source = inspect.getsource(handlers)
        assert 'callback_data=f"mhd:{day.isoformat()}:{tally.key}"' in source

    def test_back_from_a_fixture_returns_to_its_date(self) -> None:
        """Not to today. The selected date survives the return journey."""
        import inspect

        import app.bot.handlers as handlers

        source = inspect.getsource(handlers.handle_market_history_detail)
        assert 'callback_data=f"mh:day:{day.isoformat()}"' in source
