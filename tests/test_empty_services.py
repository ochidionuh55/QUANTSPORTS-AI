"""A market that found nothing is not a market we do not support.

Best of Today gives a button only to services holding a qualifying selection:
a button reading "(0)" invites a tap that leads nowhere. But absence then reads
identically to non-existence, and three different situations rendered the same
blank space — a supported market that found nothing, a market withheld from
publication, and a market that does not exist.

One line resolves it. What the line must not do is call a withheld market one
that "found nothing", because those are different states and conflating them
tells the reader something untrue.
"""

from __future__ import annotations

from datetime import date

from app.bot.formatting import format_service_menu
from app.services.best_of_day import SERVICES
from app.services.selections import WITHHELD, published_services

DAY = date(2026, 9, 19)


class TestSomeServicesEmpty:
    def test_the_line_states_how_many_found_nothing(self) -> None:
        rendered = format_service_menu({"home": 7, "draw": 2}, 36, 51, DAY, 6)
        assert "6 supported markets had no qualifying selections today" in rendered
        assert "Only markets with qualifying selections are shown" in rendered

    def test_one_empty_service_reads_singular(self) -> None:
        rendered = format_service_menu({"home": 7}, 36, 51, DAY, 1)
        assert "1 supported market had no qualifying" in rendered
        assert "markets had" not in rendered

    def test_populated_services_still_counted(self) -> None:
        rendered = format_service_menu({"home": 7, "draw": 2}, 36, 51, DAY, 6)
        assert "9 selections across 2 services" in rendered


class TestNoServicesEmpty:
    def test_no_line_when_everything_qualified(self) -> None:
        """Nothing to explain, so nothing is said."""
        rendered = format_service_menu({"home": 7, "draw": 2}, 36, 51, DAY, 0)
        assert "had no qualifying selections" not in rendered
        assert "Only markets with qualifying selections" not in rendered


class TestAllServicesEmpty:
    def test_proper_empty_state_not_an_empty_menu(self) -> None:
        rendered = format_service_menu({}, 36, 51, DAY, 20)
        assert "No selections met QUANTSPORT's qualification criteria today" in rendered
        assert "No selection was forced" in rendered

    def test_empty_state_does_not_also_show_the_partial_line(self) -> None:
        """Two messages saying the same thing is one too many."""
        rendered = format_service_menu({}, 36, 51, DAY, 20)
        assert "Only markets with qualifying selections are shown" not in rendered

    def test_modelled_count_still_shown_when_nothing_qualified(self) -> None:
        """"36 of 51 modelled, none qualified" is selectivity, not failure."""
        rendered = format_service_menu({}, 36, 51, DAY, 20)
        assert "36 of 51 fixtures modelled" in rendered


class TestWithheldServicesAreNotCountedAsEmpty:
    """A withheld market is in a different state from one that found nothing."""

    def test_withheld_services_are_excluded_from_the_count(self) -> None:
        import inspect

        import app.bot.handlers as handlers

        source = inspect.getsource(handlers.handle_best_today)
        # Counted over published_services(), which already drops WITHHELD.
        assert "published_services()" in source
        assert "empty_services" in source
        # Never counted over SERVICES, which would include withheld markets.
        assert "for key in SERVICES if not counts" not in source

    def test_published_services_excludes_withheld(self) -> None:
        publishable = set(published_services())
        for key in WITHHELD:
            assert key not in publishable

    def test_the_count_cannot_exceed_publishable_services(self) -> None:
        """Bounded by the registry, so it can never overstate."""
        assert len(list(published_services())) <= len(SERVICES)
        assert len(list(published_services())) == len(SERVICES) - len(WITHHELD)


class TestSelectedDateNotServerDate:
    def test_the_menu_shows_the_date_it_was_given(self) -> None:
        rendered = format_service_menu({"home": 1}, 1, 1, date(2026, 9, 12), 0)
        assert "Sat 12 Sep 2026" in rendered

    def test_a_past_date_does_not_claim_to_be_today(self) -> None:
        rendered = format_service_menu({"home": 1}, 1, 1, date(2026, 1, 3), 0)
        assert "📅 Today" not in rendered
        assert "Sat 3 Jan 2026" in rendered
