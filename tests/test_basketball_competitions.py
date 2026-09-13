"""Basketball competition registry tests.

The registry exists to stop one league's measured numbers being reused for
another. An NBA game totals around 225 points and a WNBA game around 160, so a
borrowed parameter set would not be slightly wrong — it would be confidently
wrong, which is the failure this whole system is built to avoid.
"""

from __future__ import annotations

import pytest

from app.core.basketball_competitions import (
    BASKETBALL_COMPETITIONS,
    Readiness,
    competition,
    parameters_for,
    publishable_competitions,
)
from app.quant.basketball import MARGIN_SIGMA, TOTAL_SIGMA


class TestReadiness:
    """Only measured competitions publish."""

    def test_only_nba_publishes_today(self) -> None:
        assert [c.key for c in publishable_competitions()] == ["NBA"]

    def test_unmeasured_competitions_refuse_parameters(self) -> None:
        """Returning a default is how an unmeasured league quietly starts
        producing numbers that look as confident as a measured one."""
        for key in ("WNBA", "NBL", "EUROLEAGUE", "FIBA_WC"):
            assert parameters_for(key) is None

    def test_nba_parameters_match_the_fitted_model(self) -> None:
        """The registry must not drift from what was actually fitted."""
        parameters = parameters_for("NBA")
        assert parameters is not None
        margin, total, _ = parameters
        assert margin == pytest.approx(MARGIN_SIGMA)
        assert total == pytest.approx(TOTAL_SIGMA)

    def test_unknown_competition_returns_nothing(self) -> None:
        assert competition("premier league") is None
        assert parameters_for("nonsense") is None

    def test_publishable_requires_every_parameter(self) -> None:
        for entry in BASKETBALL_COMPETITIONS.values():
            if entry.publishable:
                assert entry.margin_sigma is not None
                assert entry.total_sigma is not None
                assert entry.home_advantage is not None


class TestHonestStatus:
    """Every competition explains itself."""

    def test_every_competition_has_a_status_line(self) -> None:
        for entry in BASKETBALL_COMPETITIONS.values():
            assert entry.status_line

    def test_unready_competitions_state_the_blocker(self) -> None:
        """A user should be able to see why a league is missing."""
        for entry in BASKETBALL_COMPETITIONS.values():
            if entry.readiness is Readiness.PLANNED:
                assert len(entry.data_note) > 40

    def test_typical_totals_differ_across_leagues(self) -> None:
        """The reason parameters cannot be shared, encoded as data."""
        nba = competition("NBA")
        wnba = competition("WNBA")
        assert nba is not None and wnba is not None
        assert nba.typical_total is not None and wnba.typical_total is not None
        assert nba.typical_total - wnba.typical_total > 40


class TestRendering:
    """The basketball screen lists everything, honestly."""

    def test_lists_every_competition(self) -> None:
        from app.bot.formatting import format_basketball_status

        rendered = format_basketball_status(list(BASKETBALL_COMPETITIONS.values()))
        for entry in BASKETBALL_COMPETITIONS.values():
            assert entry.name in rendered

    def test_states_no_edge_is_claimed(self) -> None:
        from app.bot.formatting import format_basketball_status

        rendered = format_basketball_status(list(BASKETBALL_COMPETITIONS.values()))
        assert "no demonstrated edge" in rendered

    def test_separates_live_from_pending(self) -> None:
        from app.bot.formatting import format_basketball_status

        rendered = format_basketball_status(list(BASKETBALL_COMPETITIONS.values()))
        assert "Live" in rendered
        assert "Not yet published" in rendered
